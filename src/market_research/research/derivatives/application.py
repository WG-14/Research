"""Application authority for executable offline derivative studies.

The product modules intentionally contain pure domain functions.  This module
is the single production coordinator that binds a Research Semantics v2
preregistration, an immutable PIT dataset, the product chain, the execution
model, and the resulting immutable ``DerivativeExperimentRun``.  It never
submits an order or reaches a network; every order and fill below is a research
simulation event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias

from market_research.research.hashing import sha256_prefixed
from market_research.research.research_standard import (
    HypothesisVersion,
    Observation,
    ResearchQuestion,
    ResearchStandardError,
    ResearchStatus,
    ResearchTransition,
    assert_preregistered_before_data_access,
)

from .common import (
    DerivativeDatasetSnapshot,
    DerivativeExperimentRun,
    DerivativeExperimentSpec,
    DerivativeResearchError,
    InstrumentKind,
    RunType,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from .futures import (
    ContractChainSnapshot,
    FuturesLedger,
    FuturesLifecycleEvent,
    FuturesOrderIntent,
    FuturesSimulator,
    FuturesSpreadOrder,
    OrderSide,
    RollDecision,
    SimulationStep,
)
from .options import (
    BlackScholesModel,
    EarlyExerciseDecision,
    FillStatus,
    MultiLegExecutionPolicy,
    MultiLegOrder,
    MultiLegState,
    OptionChainSnapshot,
    OptionFill,
    OptionGreeks,
    OptionLifecycleEvent,
    OptionMark,
    OptionPosition,
    OptionSettlementInput,
    TransactionSide,
    ValuationInputSnapshot,
    execute_multi_leg_order,
    mark_option_position,
    position_from_fill,
    simulate_option_fill,
    simulate_option_lifecycle,
)
from .simulation_evidence import (
    DerivativeSimulationEvidence,
    OptionExecutionMode,
    OptionExecutionPolicy,
    OptionOrderIntentEvidence,
    futures_fill_model_hash,
)


_FAILURE_CODE = re.compile(r"[^A-Za-z0-9._:-]+")


@dataclass(frozen=True, slots=True)
class DerivativeFailureResult:
    """Persistable result artifact referenced by a failed experiment Run."""

    run_id: str
    event_stream_hash: str
    failure_code: str
    message_sha256: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.run_id, "derivative_failure_result.run_id")
        require_hash(
            self.event_stream_hash, "derivative_failure_result.event_stream_hash"
        )
        require_stable_id(self.failure_code, "derivative_failure_result.failure_code")
        require_hash(self.message_sha256, "derivative_failure_result.message_sha256")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_failed_result"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "event_stream_hash": self.event_stream_hash,
            "failure_code": self.failure_code,
            "message_sha256": self.message_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


class DerivativeApplicationError(DerivativeResearchError):
    """The coordinated study failed and carries its immutable failed Run."""

    def __init__(
        self,
        message: str,
        *,
        failed_run: DerivativeExperimentRun,
        failure_result: DerivativeFailureResult,
    ) -> None:
        super().__init__(message)
        self.failed_run = failed_run
        self.failure_result = failure_result


class ReproductionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DerivativeReproductionReceipt:
    """Result of rerunning a typed request instead of trusting stored outputs."""

    reproduction_id: str
    request_hash: str
    expected_run_hash: str
    expected_simulation_hash: str
    reproduced_run_hash: str
    reproduced_simulation_hash: str | None
    reproduced_failure_result_hash: str | None
    verified_at: str
    status: ReproductionStatus
    mismatch_fields: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.reproduction_id, "reproduction.reproduction_id")
        for value in (
            self.request_hash,
            self.expected_run_hash,
            self.expected_simulation_hash,
            self.reproduced_run_hash,
        ):
            require_hash(value, "reproduction.evidence_hash")
        for optional_hash in (
            self.reproduced_simulation_hash,
            self.reproduced_failure_result_hash,
        ):
            if optional_hash is not None:
                require_hash(optional_hash, "reproduction.result_hash")
        if (self.reproduced_simulation_hash is None) == (
            self.reproduced_failure_result_hash is None
        ):
            raise DerivativeResearchError("reproduction_result_binding_invalid")
        parse_timestamp(self.verified_at, "reproduction.verified_at")
        allowed_mismatches = {
            "preregistration_hash",
            "simulation_hash",
            "event_stream_hash",
            "run_hash",
            "reproduced_run_failed",
        }
        if (
            len(self.mismatch_fields) != len(set(self.mismatch_fields))
            or set(self.mismatch_fields) - allowed_mismatches
        ):
            raise DerivativeResearchError("reproduction_mismatch_fields_invalid")
        if (self.status is ReproductionStatus.PASS) == bool(self.mismatch_fields):
            raise DerivativeResearchError("reproduction_status_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_reproduction"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "reproduction_id": self.reproduction_id,
            "request_hash": self.request_hash,
            "expected_run_hash": self.expected_run_hash,
            "expected_simulation_hash": self.expected_simulation_hash,
            "reproduced_run_hash": self.reproduced_run_hash,
            "reproduced_simulation_hash": self.reproduced_simulation_hash,
            "reproduced_failure_result_hash": self.reproduced_failure_result_hash,
            "verified_at": self.verified_at,
            "status": self.status.value,
            "mismatch_fields": list(self.mismatch_fields),
        }

    def require_pass(self) -> None:
        if self.status is not ReproductionStatus.PASS:
            raise DerivativeResearchError(
                "derivative_reproduction_failed:" + ",".join(self.mismatch_fields)
            )


@dataclass(frozen=True, slots=True)
class ResearchPreregistration:
    """The complete knowledge objects that authorize dataset access."""

    observations: tuple[Observation, ...]
    research_question: ResearchQuestion
    hypothesis_version: HypothesisVersion
    transition: ResearchTransition

    def __post_init__(self) -> None:
        if not self.observations:
            raise DerivativeResearchError("research_observations_required")
        preregistered = parse_timestamp(
            self.transition.recorded_at,
            "research_preregistration.transition.recorded_at",
        )
        if self.hypothesis_version.preregistration_hash is None:
            raise DerivativeResearchError("research_preregistration_hash_required")
        if (
            self.transition.subject_id != self.hypothesis_version.hypothesis_id
            or self.transition.to_status is not ResearchStatus.PREREGISTERED
        ):
            raise DerivativeResearchError(
                "research_preregistration_transition_subject_status_mismatch"
            )
        if self.transition.content_hash != self.hypothesis_version.preregistration_hash:
            raise DerivativeResearchError(
                "research_preregistration_transition_hash_mismatch"
            )
        if preregistered < parse_timestamp(
            self.hypothesis_version.created_at,
            "research_preregistration.hypothesis_created_at",
        ):
            raise DerivativeResearchError("research_preregistration_before_hypothesis")
        observation_ids = [item.observation_id for item in self.observations]
        observation_hashes = tuple(item.content_hash for item in self.observations)
        if len(observation_ids) != len(set(observation_ids)):
            raise DerivativeResearchError("research_observation_id_duplicate")
        if set(observation_hashes) != set(self.research_question.observation_hashes):
            raise DerivativeResearchError("research_question_observation_mismatch")
        if (
            self.hypothesis_version.research_question_hash
            != self.research_question.content_hash
        ):
            raise DerivativeResearchError("hypothesis_research_question_mismatch")
        for observation in self.observations:
            if (
                self.research_question.research_question_id
                not in observation.linked_question_ids
            ):
                raise DerivativeResearchError(
                    "observation_research_question_link_missing"
                )
            if self.hypothesis_version.hypothesis_id not in (
                observation.linked_hypothesis_ids
            ):
                raise DerivativeResearchError("observation_hypothesis_link_missing")

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(
            {
                "observations": [item.as_dict() for item in self.observations],
                "research_question": self.research_question.as_dict(),
                "hypothesis_version": self.hypothesis_version.as_dict(),
                "transition": {
                    **self.transition.as_dict(),
                    "content_hash": self.transition.content_hash,
                },
            },
            label="derivative_research_preregistration",
        )

    def admit(
        self,
        *,
        instrument_kind: InstrumentKind,
        experiment_spec: DerivativeExperimentSpec,
        first_dataset_access_at: str,
        market_ids: tuple[str, ...],
        target_ids: tuple[str, ...],
        dataset_kinds: tuple[str, ...],
    ) -> None:
        if instrument_kind not in self.research_question.target_instrument_types:
            raise DerivativeResearchError("research_question_instrument_mismatch")
        if (
            experiment_spec.hypothesis_version_hash
            != self.hypothesis_version.content_hash
        ):
            raise DerivativeResearchError("experiment_hypothesis_version_mismatch")
        if self.research_question.target_market not in market_ids:
            raise DerivativeResearchError("research_question_market_mismatch")
        admitted_targets = set(target_ids)
        if not set(self.hypothesis_version.target_ids).issubset(admitted_targets):
            raise DerivativeResearchError("hypothesis_target_scope_mismatch")
        if any(
            not set(observation.target_ids).issubset(admitted_targets)
            for observation in self.observations
        ):
            raise DerivativeResearchError("observation_target_scope_mismatch")
        if not set(self.hypothesis_version.required_dataset_kinds).issubset(
            set(dataset_kinds)
        ):
            raise DerivativeResearchError("hypothesis_dataset_kind_mismatch")
        if experiment_spec.run_type in {
            RunType.CONFIRMATORY,
            RunType.ROBUSTNESS,
            RunType.PROSPECTIVE,
        }:
            preregistered = parse_timestamp(
                self.transition.recorded_at,
                "research_preregistration.transition.recorded_at",
            )
            frozen = parse_timestamp(
                experiment_spec.frozen_at,
                "research_preregistration.experiment_frozen_at",
            )
            first_access = parse_timestamp(
                first_dataset_access_at,
                "research_preregistration.first_dataset_access_at",
            )
            if preregistered > frozen or frozen > first_access:
                raise DerivativeResearchError(
                    "research_preregistration_freeze_access_order_invalid"
                )
            try:
                assert_preregistered_before_data_access(
                    self.hypothesis_version,
                    first_confirmation_access_at=first_dataset_access_at,
                )
            except ResearchStandardError as exc:
                raise DerivativeResearchError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class FuturesOrderCommand:
    intent: FuturesOrderIntent
    fill_id: str
    step_id: str

    def __post_init__(self) -> None:
        require_stable_id(self.fill_id, "futures_command.fill_id")
        require_stable_id(self.step_id, "futures_command.step_id")


@dataclass(frozen=True, slots=True)
class FuturesSettlementCommand:
    contract_id: str
    as_of: str
    event_id: str
    step_id: str

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "futures_command.contract_id")
        parse_timestamp(self.as_of, "futures_command.as_of")
        require_stable_id(self.event_id, "futures_command.event_id")
        require_stable_id(self.step_id, "futures_command.step_id")


@dataclass(frozen=True, slots=True)
class FuturesRollCommand:
    decision: RollDecision
    execution_id: str
    step_id: str

    def __post_init__(self) -> None:
        require_stable_id(self.execution_id, "futures_command.execution_id")
        require_stable_id(self.step_id, "futures_command.step_id")


@dataclass(frozen=True, slots=True)
class FuturesExpirationCommand:
    contract_id: str
    as_of: str
    event_id: str
    step_id: str

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "futures_command.contract_id")
        parse_timestamp(self.as_of, "futures_command.as_of")
        require_stable_id(self.event_id, "futures_command.event_id")
        require_stable_id(self.step_id, "futures_command.step_id")


@dataclass(frozen=True, slots=True)
class FuturesSpreadCommand:
    order: FuturesSpreadOrder
    execution_id: str
    step_id: str

    def __post_init__(self) -> None:
        require_stable_id(self.execution_id, "futures_command.execution_id")
        require_stable_id(self.step_id, "futures_command.step_id")


FuturesCommand: TypeAlias = (
    FuturesOrderCommand
    | FuturesSettlementCommand
    | FuturesRollCommand
    | FuturesExpirationCommand
    | FuturesSpreadCommand
)


def _futures_executed_contract_ids(
    commands: tuple[FuturesCommand, ...],
) -> tuple[str, ...]:
    contract_ids: set[str] = set()
    for command in commands:
        if isinstance(command, FuturesOrderCommand):
            contract_ids.add(command.intent.contract_id)
        elif isinstance(command, (FuturesSettlementCommand, FuturesExpirationCommand)):
            contract_ids.add(command.contract_id)
        elif isinstance(command, FuturesRollCommand):
            contract_ids.update(
                {
                    command.decision.from_contract_id,
                    command.decision.to_contract_id,
                }
            )
        elif isinstance(command, FuturesSpreadCommand):
            contract_ids.update(item.contract_id for item in command.order.legs)
    return tuple(sorted(contract_ids))


@dataclass(frozen=True, slots=True)
class FuturesStudyRequest:
    run_id: str
    simulation_id: str
    ledger_id: str
    started_at: str
    finished_at: str
    initial_cash: Decimal
    preregistration: ResearchPreregistration
    dataset: DerivativeDatasetSnapshot
    experiment_spec: DerivativeExperimentSpec
    chain: ContractChainSnapshot
    simulator: FuturesSimulator
    commands: tuple[FuturesCommand, ...]
    lifecycle_events: tuple[FuturesLifecycleEvent, ...] = ()

    def __post_init__(self) -> None:
        for field_name, value in (
            ("run_id", self.run_id),
            ("simulation_id", self.simulation_id),
            ("ledger_id", self.ledger_id),
        ):
            require_stable_id(value, f"futures_study.{field_name}")
        started = parse_timestamp(self.started_at, "futures_study.started_at")
        finished = parse_timestamp(self.finished_at, "futures_study.finished_at")
        if finished < started:
            raise DerivativeResearchError("futures_study_time_order_invalid")
        initial_cash = exact_decimal(
            self.initial_cash, "futures_study.initial_cash", positive=True
        )
        object.__setattr__(self, "initial_cash", initial_cash)
        if not self.commands:
            raise DerivativeResearchError("futures_study_commands_required")
        step_ids = [item.step_id for item in self.commands]
        if len(step_ids) != len(set(step_ids)):
            raise DerivativeResearchError("futures_study_step_id_duplicate")
        command_ids: list[str] = []
        command_times: list[str] = []
        for command in self.commands:
            if isinstance(command, FuturesOrderCommand):
                command_ids.append(command.fill_id)
                command_times.append(command.intent.decision_at)
            elif isinstance(command, FuturesSettlementCommand):
                command_ids.append(command.event_id)
                command_times.append(command.as_of)
            elif isinstance(command, FuturesRollCommand):
                command_ids.append(command.execution_id)
                command_times.append(command.decision.decision_at)
            elif isinstance(command, FuturesExpirationCommand):
                command_ids.append(command.event_id)
                command_times.append(command.as_of)
            elif isinstance(command, FuturesSpreadCommand):
                command_ids.append(command.execution_id)
                command_times.append(command.order.decision_at)
            else:
                raise DerivativeResearchError("futures_study_command_type_invalid")
        if len(command_ids) != len(set(command_ids)):
            raise DerivativeResearchError("futures_study_command_id_duplicate")
        if any(
            not started
            <= parse_timestamp(item, "futures_study.command_time")
            <= finished
            for item in command_times
        ):
            raise DerivativeResearchError("futures_study_command_outside_run")
        parsed_command_times = tuple(
            parse_timestamp(item, "futures_study.command_time")
            for item in command_times
        )
        if parsed_command_times != tuple(sorted(parsed_command_times)):
            raise DerivativeResearchError("futures_study_command_time_not_monotonic")
        lifecycle_ids: set[str] = set()
        chain_contract_ids = {item.contract_id for item in self.chain.contracts}
        chain_lifecycle_hashes = {
            item.content_hash for item in self.chain.lifecycle_events
        }
        chain_source_hashes = set(self.chain.source_manifest_hashes)
        dataset_source_hashes = set(self.dataset.raw_manifest_hashes)
        dataset_start = parse_timestamp(
            self.dataset.period_start, "futures_study.dataset_period_start"
        )
        dataset_end = parse_timestamp(
            self.dataset.period_end, "futures_study.dataset_period_end"
        )
        for event in self.chain.lifecycle_events:
            if event.source_hash not in dataset_source_hashes:
                raise DerivativeResearchError(
                    "futures_study_chain_lifecycle_source_not_in_dataset"
                )
            event_at = parse_timestamp(
                event.event_at, "futures_study.chain_lifecycle_event_at"
            )
            if not dataset_start <= event_at <= dataset_end:
                raise DerivativeResearchError(
                    "futures_study_chain_lifecycle_outside_dataset_period"
                )
            if not event.availability.known_at(self.dataset.knowledge_time):
                raise DerivativeResearchError(
                    "futures_study_chain_lifecycle_unknown_at_dataset_knowledge_time"
                )
        for event in self.lifecycle_events:
            if not isinstance(event, FuturesLifecycleEvent):
                raise DerivativeResearchError("futures_study_lifecycle_type_invalid")
            if event.event_id in lifecycle_ids:
                raise DerivativeResearchError("futures_study_lifecycle_id_duplicate")
            lifecycle_ids.add(event.event_id)
            if event.contract_id not in chain_contract_ids:
                raise DerivativeResearchError(
                    "futures_study_lifecycle_contract_unknown"
                )
            if event.source_hash not in chain_source_hashes:
                raise DerivativeResearchError(
                    "futures_study_lifecycle_source_not_in_chain"
                )
            if event.source_hash not in dataset_source_hashes:
                raise DerivativeResearchError(
                    "futures_study_lifecycle_source_not_in_dataset"
                )
            event_at = parse_timestamp(
                event.event_at, "futures_study.lifecycle_event_at"
            )
            if not dataset_start <= event_at <= dataset_end:
                raise DerivativeResearchError(
                    "futures_study_lifecycle_outside_dataset_period"
                )
            if not event.availability.known_at(self.dataset.knowledge_time):
                raise DerivativeResearchError(
                    "futures_study_lifecycle_unknown_at_dataset_knowledge_time"
                )
            if event.content_hash not in chain_lifecycle_hashes:
                raise DerivativeResearchError("futures_study_lifecycle_not_in_chain")
            if not started <= event_at <= finished:
                raise DerivativeResearchError("futures_study_lifecycle_outside_run")
            if not event.availability.known_at(self.finished_at):
                raise DerivativeResearchError(
                    "futures_study_lifecycle_future_knowledge"
                )


@dataclass(frozen=True, slots=True)
class OptionLifecycleCommand:
    event_id: str
    event_at: str
    settlement_input: OptionSettlementInput
    observation_dataset_hash: str
    exercise_fraction: Decimal = Decimal("1")
    early_exercise_decision: EarlyExerciseDecision | None = None

    def __post_init__(self) -> None:
        require_stable_id(self.event_id, "option_lifecycle_command.event_id")
        parse_timestamp(self.event_at, "option_lifecycle_command.event_at")
        if not isinstance(self.settlement_input, OptionSettlementInput):
            raise DerivativeResearchError(
                "option_lifecycle_command_settlement_input_required"
            )
        self.settlement_input.require_known_at(self.event_at)
        require_hash(
            self.observation_dataset_hash,
            "option_lifecycle_command.observation_dataset_hash",
        )
        fraction = exact_decimal(
            self.exercise_fraction, "option_lifecycle_command.exercise_fraction"
        )
        if fraction < 0 or fraction > 1:
            raise DerivativeResearchError("option_lifecycle_fraction_invalid")
        object.__setattr__(self, "exercise_fraction", fraction)


@dataclass(frozen=True, slots=True)
class OptionOrderCommand:
    order_id: str
    position_id: str
    contract_id: str
    side: TransactionSide
    quantity: Decimal
    requested_at: str
    valuation_input: ValuationInputSnapshot
    participation_rate: Decimal = Decimal("1")
    lifecycle: OptionLifecycleCommand | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("order_id", self.order_id),
            ("position_id", self.position_id),
            ("contract_id", self.contract_id),
        ):
            require_stable_id(value, f"option_order_command.{field_name}")
        if not isinstance(self.side, TransactionSide):
            raise DerivativeResearchError("option_order_command_side_invalid")
        parse_timestamp(self.requested_at, "option_order_command.requested_at")
        quantity = exact_decimal(
            self.quantity, "option_order_command.quantity", positive=True
        )
        participation = exact_decimal(
            self.participation_rate,
            "option_order_command.participation_rate",
            positive=True,
        )
        if participation > 1:
            raise DerivativeResearchError("option_order_participation_invalid")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "participation_rate", participation)


@dataclass(frozen=True, slots=True)
class OptionStudyRequest:
    run_id: str
    simulation_id: str
    started_at: str
    finished_at: str
    preregistration: ResearchPreregistration
    dataset: DerivativeDatasetSnapshot
    experiment_spec: DerivativeExperimentSpec
    chain: OptionChainSnapshot
    execution_policy: OptionExecutionPolicy
    valuation_model: BlackScholesModel
    orders: tuple[OptionOrderCommand, ...]
    lifecycle_datasets: tuple[DerivativeDatasetSnapshot, ...] = ()

    def __post_init__(self) -> None:
        require_stable_id(self.run_id, "option_study.run_id")
        require_stable_id(self.simulation_id, "option_study.simulation_id")
        started = parse_timestamp(self.started_at, "option_study.started_at")
        finished = parse_timestamp(self.finished_at, "option_study.finished_at")
        if finished < started:
            raise DerivativeResearchError("option_study_time_order_invalid")
        if not self.orders:
            raise DerivativeResearchError("option_study_orders_required")
        order_ids = [item.order_id for item in self.orders]
        position_ids = [item.position_id for item in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise DerivativeResearchError("option_study_order_id_duplicate")
        if len(position_ids) != len(set(position_ids)):
            raise DerivativeResearchError("option_study_position_id_duplicate")
        requested_times: list[datetime] = []
        lifecycle_ids: set[str] = set()
        for order in self.orders:
            request_time = parse_timestamp(
                order.requested_at, "option_study.order_requested_at"
            )
            valuation_time = parse_timestamp(
                order.valuation_input.valuation_at,
                "option_study.valuation_at",
            )
            if not started <= request_time <= valuation_time <= finished:
                raise DerivativeResearchError("option_study_event_outside_run")
            requested_times.append(request_time)
            if order.lifecycle is not None and not (
                valuation_time
                <= parse_timestamp(
                    order.lifecycle.event_at,
                    "option_study.lifecycle_event_at",
                )
                <= finished
            ):
                raise DerivativeResearchError("option_study_lifecycle_outside_run")
            if order.lifecycle is not None:
                if order.lifecycle.event_id in lifecycle_ids:
                    raise DerivativeResearchError("option_study_lifecycle_id_duplicate")
                lifecycle_ids.add(order.lifecycle.event_id)
        if tuple(requested_times) != tuple(sorted(requested_times)):
            raise DerivativeResearchError("option_study_order_time_not_monotonic")
        lifecycle_hashes = tuple(
            item.lifecycle.observation_dataset_hash
            for item in self.orders
            if item.lifecycle is not None
        )
        if any(
            not isinstance(item, DerivativeDatasetSnapshot)
            for item in self.lifecycle_datasets
        ):
            raise DerivativeResearchError("option_study_lifecycle_dataset_invalid")
        dataset_hashes = tuple(item.content_hash for item in self.lifecycle_datasets)
        if len(dataset_hashes) != len(set(dataset_hashes)):
            raise DerivativeResearchError("option_study_lifecycle_dataset_duplicate")
        if set(lifecycle_hashes) != set(dataset_hashes):
            raise DerivativeResearchError(
                "option_study_lifecycle_dataset_binding_required"
            )


@dataclass(frozen=True, slots=True)
class MultiLegStudyRequest:
    run_id: str
    simulation_id: str
    started_at: str
    finished_at: str
    preregistration: ResearchPreregistration
    dataset: DerivativeDatasetSnapshot
    experiment_spec: DerivativeExperimentSpec
    chain: OptionChainSnapshot
    execution_policy: OptionExecutionPolicy
    valuation_model: BlackScholesModel
    order: MultiLegOrder
    valuation_inputs: tuple[ValuationInputSnapshot, ...]
    fill_times: tuple[tuple[str, str], ...]
    participation_rates: tuple[tuple[str, Decimal], ...] = ()
    lifecycle_by_contract: tuple[tuple[str, OptionLifecycleCommand], ...] = ()
    lifecycle_datasets: tuple[DerivativeDatasetSnapshot, ...] = ()

    def __post_init__(self) -> None:
        require_stable_id(self.run_id, "multileg_study.run_id")
        require_stable_id(self.simulation_id, "multileg_study.simulation_id")
        started = parse_timestamp(self.started_at, "multileg_study.started_at")
        finished = parse_timestamp(self.finished_at, "multileg_study.finished_at")
        if finished < started:
            raise DerivativeResearchError("multileg_study_time_order_invalid")
        if not self.valuation_inputs:
            raise DerivativeResearchError("multileg_valuation_inputs_required")
        if self.order.execution_policy_hash != self.execution_policy.content_hash:
            raise DerivativeResearchError("multileg_order_execution_policy_mismatch")
        if self.order.allow_partial != self.execution_policy.allow_partial:
            raise DerivativeResearchError("multileg_order_partial_policy_mismatch")
        if (
            self.order.maximum_leg_time_skew_seconds
            != self.execution_policy.maximum_leg_time_skew_seconds
        ):
            raise DerivativeResearchError("multileg_order_time_skew_policy_mismatch")
        expected_order_policy = (
            MultiLegExecutionPolicy.SIMULTANEOUS
            if self.execution_policy.mode is OptionExecutionMode.SIMULTANEOUS
            else MultiLegExecutionPolicy.SEQUENTIAL
        )
        if self.order.policy is not expected_order_policy:
            raise DerivativeResearchError("multileg_order_mode_policy_mismatch")

        leg_ids = {item.leg_id for item in self.order.legs}
        contract_ids = {item.contract.contract_id for item in self.order.legs}
        input_contract_ids = [
            item.contract.contract_id for item in self.valuation_inputs
        ]
        if len(input_contract_ids) != len(set(input_contract_ids)):
            raise DerivativeResearchError("multileg_valuation_input_duplicate")
        if set(input_contract_ids) != contract_ids:
            raise DerivativeResearchError("multileg_valuation_input_coverage_mismatch")

        fill_leg_ids = [leg_id for leg_id, _value in self.fill_times]
        if len(fill_leg_ids) != len(set(fill_leg_ids)):
            raise DerivativeResearchError("multileg_fill_time_leg_duplicate")
        if set(fill_leg_ids) != leg_ids:
            raise DerivativeResearchError("multileg_fill_time_coverage_mismatch")
        requested_at = parse_timestamp(
            self.order.requested_at, "multileg_study.requested_at"
        )
        if not started <= requested_at <= finished:
            raise DerivativeResearchError("multileg_order_outside_run")
        for _leg_id, fill_time in self.fill_times:
            parsed_fill = parse_timestamp(fill_time, "multileg_study.fill_time")
            if not requested_at <= parsed_fill <= finished:
                raise DerivativeResearchError("multileg_fill_time_outside_run")

        participation_leg_ids = [leg_id for leg_id, _value in self.participation_rates]
        if len(participation_leg_ids) != len(set(participation_leg_ids)):
            raise DerivativeResearchError("multileg_participation_leg_duplicate")
        if not set(participation_leg_ids).issubset(leg_ids):
            raise DerivativeResearchError("multileg_participation_leg_unknown")
        for _leg_id, participation_rate in self.participation_rates:
            rate = exact_decimal(
                participation_rate,
                "multileg_study.participation_rate",
                positive=True,
            )
            if rate > 1:
                raise DerivativeResearchError("multileg_participation_rate_invalid")

        lifecycle_contract_ids = [
            contract_id for contract_id, _value in self.lifecycle_by_contract
        ]
        if len(lifecycle_contract_ids) != len(set(lifecycle_contract_ids)):
            raise DerivativeResearchError("multileg_lifecycle_contract_duplicate")
        if not set(lifecycle_contract_ids).issubset(contract_ids):
            raise DerivativeResearchError("multileg_lifecycle_contract_unknown")
        if any(
            not requested_at
            <= parse_timestamp(item.valuation_at, "multileg_study.valuation_at")
            <= finished
            for item in self.valuation_inputs
        ):
            raise DerivativeResearchError("multileg_valuation_outside_run")
        if any(
            not requested_at
            <= parse_timestamp(item.event_at, "multileg_study.lifecycle_event_at")
            <= finished
            for _contract_id, item in self.lifecycle_by_contract
        ):
            raise DerivativeResearchError("multileg_lifecycle_outside_run")
        lifecycle_hashes = tuple(
            item.observation_dataset_hash
            for _contract_id, item in self.lifecycle_by_contract
        )
        if any(
            not isinstance(item, DerivativeDatasetSnapshot)
            for item in self.lifecycle_datasets
        ):
            raise DerivativeResearchError("multileg_study_lifecycle_dataset_invalid")
        dataset_hashes = tuple(item.content_hash for item in self.lifecycle_datasets)
        if len(dataset_hashes) != len(set(dataset_hashes)):
            raise DerivativeResearchError("multileg_study_lifecycle_dataset_duplicate")
        if set(lifecycle_hashes) != set(dataset_hashes):
            raise DerivativeResearchError(
                "multileg_study_lifecycle_dataset_binding_required"
            )


@dataclass(frozen=True, slots=True)
class DerivativeStudyExecution:
    preregistration_hash: str
    simulation: DerivativeSimulationEvidence
    run: DerivativeExperimentRun

    def __post_init__(self) -> None:
        require_hash(
            self.preregistration_hash, "derivative_execution.preregistration_hash"
        )
        if self.run.status != "SUCCEEDED":
            raise DerivativeResearchError("derivative_execution_run_not_succeeded")
        self.simulation.validate_run(self.run)


class DerivativeResearchApplicationService:
    """Run typed derivative simulations through one fail-closed authority."""

    def reproduce_futures(
        self,
        request: FuturesStudyRequest,
        expected: DerivativeStudyExecution,
        *,
        reproduction_id: str,
        verified_at: str,
    ) -> DerivativeReproductionReceipt:
        """Rerun Futures calculations from the typed immutable request."""

        request_hash = _futures_request_hash(request)
        try:
            reproduced = self.run_futures(request)
        except DerivativeApplicationError as exc:
            return self._failed_reproduction(
                request_hash=request_hash,
                expected=expected,
                failure=exc,
                reproduction_id=reproduction_id,
                verified_at=verified_at,
            )
        return self._reproduce(
            request_hash=request_hash,
            expected=expected,
            reproduced=reproduced,
            reproduction_id=reproduction_id,
            verified_at=verified_at,
        )

    def reproduce_option(
        self,
        request: OptionStudyRequest,
        expected: DerivativeStudyExecution,
        *,
        reproduction_id: str,
        verified_at: str,
    ) -> DerivativeReproductionReceipt:
        """Rerun single-leg Option calculations from the typed request."""

        request_hash = _option_request_hash(request)
        try:
            reproduced = self.run_option(request)
        except DerivativeApplicationError as exc:
            return self._failed_reproduction(
                request_hash=request_hash,
                expected=expected,
                failure=exc,
                reproduction_id=reproduction_id,
                verified_at=verified_at,
            )
        return self._reproduce(
            request_hash=request_hash,
            expected=expected,
            reproduced=reproduced,
            reproduction_id=reproduction_id,
            verified_at=verified_at,
        )

    def reproduce_multi_leg(
        self,
        request: MultiLegStudyRequest,
        expected: DerivativeStudyExecution,
        *,
        reproduction_id: str,
        verified_at: str,
    ) -> DerivativeReproductionReceipt:
        """Rerun multi-leg fills and valuations from the typed request."""

        request_hash = _multileg_request_hash(request)
        try:
            reproduced = self.run_multi_leg(request)
        except DerivativeApplicationError as exc:
            return self._failed_reproduction(
                request_hash=request_hash,
                expected=expected,
                failure=exc,
                reproduction_id=reproduction_id,
                verified_at=verified_at,
            )
        return self._reproduce(
            request_hash=request_hash,
            expected=expected,
            reproduced=reproduced,
            reproduction_id=reproduction_id,
            verified_at=verified_at,
        )

    def run_futures(self, request: FuturesStudyRequest) -> DerivativeStudyExecution:
        """Execute Futures research and publish every domain failure as a failed Run."""

        try:
            return self._run_futures(request)
        except DerivativeApplicationError:
            raise
        except DerivativeResearchError as exc:
            raise self._failure(
                request.run_id,
                request.experiment_spec,
                request.dataset,
                request.started_at,
                request.finished_at,
                exc,
            ) from exc

    def _run_futures(self, request: FuturesStudyRequest) -> DerivativeStudyExecution:
        executed_contract_ids = tuple(
            sorted(
                {
                    *_futures_executed_contract_ids(request.commands),
                    *(item.contract_id for item in request.lifecycle_events),
                }
            )
        )
        self._admit_common(
            preregistration=request.preregistration,
            instrument_kind=InstrumentKind.FUTURE,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            chain_hash=request.chain.content_hash,
            chain_knowledge_time=request.chain.observed_at,
            chain_source_manifest_hashes=request.chain.source_manifest_hashes,
            market_ids=(request.chain.root_id,),
            target_ids=(
                request.chain.root_id,
                *executed_contract_ids,
            ),
            contract_ids=tuple(item.contract_id for item in request.chain.contracts),
            dataset_kinds=("point_in_time_chain", "futures_contract_chain"),
            first_access_at=request.started_at,
        )
        request.chain.admit(request.experiment_spec.run_type)
        if (
            request.experiment_spec.simulation_policy_hash
            != request.simulator.content_hash
        ):
            raise DerivativeResearchError("futures_study_simulator_mismatch")
        if (
            request.experiment_spec.cost_model_hash
            != request.simulator.cost_policy.content_hash
        ):
            raise DerivativeResearchError("futures_study_cost_model_mismatch")
        if request.experiment_spec.fill_model_hash != futures_fill_model_hash(
            request.simulator
        ):
            raise DerivativeResearchError("futures_study_fill_model_mismatch")
        if request.experiment_spec.valuation_model_hash is not None:
            raise DerivativeResearchError("futures_study_valuation_model_forbidden")
        simulator_ids = {item.contract_id for item in request.simulator.contracts}
        chain_ids = {item.contract_id for item in request.chain.contracts}
        if not simulator_ids.issubset(chain_ids):
            raise DerivativeResearchError("futures_study_contract_not_in_chain")

        ledger = FuturesLedger.open(request.ledger_id, request.initial_cash)
        orders: list[FuturesOrderIntent] = []
        steps: list[SimulationStep] = []
        try:
            for command in request.commands:
                if isinstance(command, FuturesOrderCommand):
                    quote = request.chain.quote_for(
                        command.intent.contract_id, command.intent.decision_at
                    )
                    step = request.simulator.execute(
                        ledger,
                        command.intent,
                        quote,
                        fill_id=command.fill_id,
                        step_id=command.step_id,
                    )
                    orders.append(command.intent)
                elif isinstance(command, FuturesSettlementCommand):
                    quote = request.chain.quote_for(command.contract_id, command.as_of)
                    step = request.simulator.settle_daily(
                        ledger,
                        quote,
                        event_id=command.event_id,
                        step_id=command.step_id,
                        as_of=command.as_of,
                    )
                elif isinstance(command, FuturesRollCommand):
                    old_quote = request.chain.quote_for(
                        command.decision.from_contract_id,
                        command.decision.decision_at,
                    )
                    new_quote = request.chain.quote_for(
                        command.decision.to_contract_id,
                        command.decision.decision_at,
                    )
                    position = ledger.position_for(command.decision.from_contract_id)
                    if position is None:
                        raise DerivativeResearchError(
                            "roll_execution_source_position_missing"
                        )
                    close_side = (
                        OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
                    )
                    open_side = (
                        OrderSide.BUY if position.quantity > 0 else OrderSide.SELL
                    )
                    orders.extend(
                        (
                            FuturesOrderIntent(
                                intent_id=f"{command.execution_id}.close",
                                contract_id=command.decision.from_contract_id,
                                side=close_side,
                                quantity=abs(position.quantity),
                                decision_at=command.decision.decision_at,
                            ),
                            FuturesOrderIntent(
                                intent_id=f"{command.execution_id}.open",
                                contract_id=command.decision.to_contract_id,
                                side=open_side,
                                quantity=abs(position.quantity),
                                decision_at=command.decision.decision_at,
                            ),
                        )
                    )
                    step = request.simulator.roll(
                        ledger,
                        command.decision,
                        old_quote,
                        new_quote,
                        execution_id=command.execution_id,
                        step_id=command.step_id,
                    )
                elif isinstance(command, FuturesExpirationCommand):
                    quote = request.chain.quote_for(command.contract_id, command.as_of)
                    step = request.simulator.handle_expiration(
                        ledger,
                        quote,
                        event_id=command.event_id,
                        step_id=command.step_id,
                    )
                else:
                    quotes = tuple(
                        request.chain.quote_for(
                            leg.contract_id, command.order.decision_at
                        )
                        for leg in command.order.legs
                    )
                    step, _execution = request.simulator.execute_spread(
                        ledger,
                        command.order,
                        quotes,
                        execution_id=command.execution_id,
                        step_id=command.step_id,
                    )
                    for index, leg in enumerate(command.order.legs):
                        orders.append(
                            FuturesOrderIntent(
                                intent_id=f"{command.order.order_id}.leg{index}",
                                contract_id=leg.contract_id,
                                side=OrderSide.BUY if leg.ratio > 0 else OrderSide.SELL,
                                quantity=abs(leg.ratio) * command.order.units,
                                decision_at=command.order.decision_at,
                            )
                        )
                ledger = step.ledger
                if ledger.failed:
                    raise DerivativeResearchError("futures_ledger_failed")
                steps.append(step)
        except DerivativeResearchError as exc:
            raise self._failure(
                request.run_id,
                request.experiment_spec,
                request.dataset,
                request.started_at,
                request.finished_at,
                exc,
            ) from exc

        simulation = DerivativeSimulationEvidence.from_futures(
            simulation_id=request.simulation_id,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            chain=request.chain,
            simulator=request.simulator,
            orders=orders,
            steps=steps,
            lifecycle_events=request.lifecycle_events,
        )
        return self._success(
            run_id=request.run_id,
            preregistration=request.preregistration,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            simulation=simulation,
            started_at=request.started_at,
            finished_at=request.finished_at,
        )

    def run_option(self, request: OptionStudyRequest) -> DerivativeStudyExecution:
        """Execute an Option study and preserve admission failures as evidence."""

        try:
            return self._run_option(request)
        except DerivativeApplicationError:
            raise
        except DerivativeResearchError as exc:
            raise self._failure_from_option(request, exc) from exc

    def _run_option(self, request: OptionStudyRequest) -> DerivativeStudyExecution:
        if request.execution_policy.mode is not OptionExecutionMode.SINGLE:
            raise DerivativeResearchError("single_option_execution_mode_required")
        self._admit_option(request)
        lifecycle_datasets = {
            item.content_hash: item for item in request.lifecycle_datasets
        }
        orders: list[OptionOrderIntentEvidence] = []
        fills: list[OptionFill] = []
        positions: list[OptionPosition] = []
        inputs: list[ValuationInputSnapshot] = []
        ivs = []
        greeks: list[OptionGreeks] = []
        marks: list[OptionMark] = []
        lifecycle_events: list[OptionLifecycleEvent] = []
        lifecycle_observation_hashes: list[str] = []
        try:
            for command in request.orders:
                contract = request.chain.contract(command.contract_id)
                quote = request.chain.quote(command.contract_id)
                self._validate_valuation_input(
                    command.valuation_input,
                    contract.content_hash,
                    quote.content_hash,
                    request.dataset,
                )
                fill = simulate_option_fill(
                    fill_id=command.order_id,
                    contract=contract,
                    quote=quote,
                    side=command.side,
                    quantity=command.quantity,
                    filled_at=command.requested_at,
                    participation_rate=command.participation_rate,
                    fee_per_contract=request.execution_policy.fee_per_contract,
                    slippage_ticks=request.execution_policy.slippage_ticks,
                    allow_partial=request.execution_policy.allow_partial,
                    allow_illiquid=request.execution_policy.allow_illiquid,
                )
                if fill.status not in {FillStatus.FILLED, FillStatus.PARTIAL}:
                    raise DerivativeResearchError(
                        f"option_fill_not_executed:{fill.failure_code or 'unknown'}"
                    )
                order = OptionOrderIntentEvidence(
                    order_id=command.order_id,
                    contract_id=command.contract_id,
                    side=command.side,
                    quantity=command.quantity,
                    requested_at=command.requested_at,
                    quote_hash=quote.content_hash,
                    execution_policy_hash=request.execution_policy.content_hash,
                )
                position = position_from_fill(fill, position_id=command.position_id)
                iv = request.valuation_model.implied_volatility(
                    command.valuation_input,
                    permit_illiquid=request.execution_policy.allow_illiquid,
                )
                if not iv.success or iv.volatility is None:
                    raise DerivativeResearchError(
                        f"option_iv_required:{iv.failure.value}"
                    )
                greek = request.valuation_model.greeks(
                    command.valuation_input, iv.volatility
                )
                mark = mark_option_position(
                    position,
                    quote=quote,
                    theoretical_price=greek.price,
                    theoretical_input_hash=command.valuation_input.content_hash,
                    marked_at=command.valuation_input.valuation_at,
                    allow_illiquid=request.execution_policy.allow_illiquid,
                )
                if command.lifecycle is not None:
                    self._validate_settlement_input(
                        command.lifecycle.settlement_input,
                        contract_id=contract.contract_id,
                        dataset=lifecycle_datasets.get(
                            command.lifecycle.observation_dataset_hash
                        ),
                    )
                    lifecycle_events.append(
                        simulate_option_lifecycle(
                            position,
                            event_id=command.lifecycle.event_id,
                            event_at=command.lifecycle.event_at,
                            settlement_input=command.lifecycle.settlement_input,
                            exercise_fraction=command.lifecycle.exercise_fraction,
                            early_exercise_decision=(
                                command.lifecycle.early_exercise_decision
                            ),
                        )
                    )
                    lifecycle_observation_hashes.append(
                        command.lifecycle.observation_dataset_hash
                    )
                orders.append(order)
                fills.append(fill)
                positions.append(position)
                inputs.append(command.valuation_input)
                ivs.append(iv)
                greeks.append(greek)
                marks.append(mark)
        except DerivativeResearchError as exc:
            raise self._failure_from_option(request, exc) from exc

        simulation = DerivativeSimulationEvidence.from_option(
            simulation_id=request.simulation_id,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            chain=request.chain,
            execution_policy=request.execution_policy,
            valuation_model=request.valuation_model,
            orders=orders,
            fills=fills,
            positions=positions,
            valuation_inputs=inputs,
            implied_volatilities=ivs,
            greeks=greeks,
            marks=marks,
            lifecycle_events=lifecycle_events,
            lifecycle_datasets=request.lifecycle_datasets,
            lifecycle_observation_dataset_hashes=lifecycle_observation_hashes,
        )
        return self._success(
            run_id=request.run_id,
            preregistration=request.preregistration,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            simulation=simulation,
            started_at=request.started_at,
            finished_at=request.finished_at,
            observation_dataset_snapshot_hashes=tuple(
                item.content_hash for item in request.lifecycle_datasets
            ),
        )

    def run_multi_leg(self, request: MultiLegStudyRequest) -> DerivativeStudyExecution:
        """Execute a multi-leg study and preserve admission failures as evidence."""

        try:
            return self._run_multi_leg(request)
        except DerivativeApplicationError:
            raise
        except DerivativeResearchError as exc:
            raise self._failure_from_option(request, exc) from exc

    def _run_multi_leg(self, request: MultiLegStudyRequest) -> DerivativeStudyExecution:
        if request.execution_policy.mode is OptionExecutionMode.SINGLE:
            raise DerivativeResearchError("multileg_option_execution_mode_required")
        self._admit_option(request)
        lifecycle_datasets = {
            item.content_hash: item for item in request.lifecycle_datasets
        }
        inputs_by_contract = {
            item.contract.contract_id: item for item in request.valuation_inputs
        }
        quotes = {
            leg.contract.contract_id: request.chain.quote(leg.contract.contract_id)
            for leg in request.order.legs
        }
        fill_times = dict(request.fill_times)
        participation = {
            key: exact_decimal(value, "multileg_study.participation_rate")
            for key, value in request.participation_rates
        }
        lifecycle_by_contract = dict(request.lifecycle_by_contract)
        try:
            result = execute_multi_leg_order(
                request.order,
                quotes=quotes,
                fill_times=fill_times,
                participation_rates=participation,
                fee_per_contract=request.execution_policy.fee_per_contract,
                slippage_ticks=request.execution_policy.slippage_ticks,
                allow_illiquid=request.execution_policy.allow_illiquid,
            )
            if not result.committed_fills:
                raise DerivativeResearchError(
                    f"multileg_not_executed:{result.failure_code or 'unknown'}"
                )
            if (
                result.state is MultiLegState.PARTIAL
                and not request.order.allow_partial
            ):
                raise DerivativeResearchError("multileg_partial_execution_forbidden")
            positions: list[OptionPosition] = []
            inputs: list[ValuationInputSnapshot] = []
            ivs = []
            greeks: list[OptionGreeks] = []
            marks: list[OptionMark] = []
            lifecycle_events: list[OptionLifecycleEvent] = []
            lifecycle_observation_hashes: list[str] = []
            for index, fill in enumerate(result.committed_fills):
                valuation = inputs_by_contract.get(fill.contract.contract_id)
                if valuation is None:
                    raise DerivativeResearchError("multileg_valuation_input_missing")
                quote = quotes[fill.contract.contract_id]
                self._validate_valuation_input(
                    valuation,
                    fill.contract.content_hash,
                    quote.content_hash,
                    request.dataset,
                )
                position = position_from_fill(
                    fill, position_id=f"{request.order.group_id}.position{index}"
                )
                iv = request.valuation_model.implied_volatility(
                    valuation,
                    permit_illiquid=request.execution_policy.allow_illiquid,
                )
                if not iv.success or iv.volatility is None:
                    raise DerivativeResearchError(
                        f"option_iv_required:{iv.failure.value}"
                    )
                greek = request.valuation_model.greeks(valuation, iv.volatility)
                mark = mark_option_position(
                    position,
                    quote=quote,
                    theoretical_price=greek.price,
                    theoretical_input_hash=valuation.content_hash,
                    marked_at=valuation.valuation_at,
                    allow_illiquid=request.execution_policy.allow_illiquid,
                )
                lifecycle = lifecycle_by_contract.get(fill.contract.contract_id)
                if lifecycle is not None:
                    self._validate_settlement_input(
                        lifecycle.settlement_input,
                        contract_id=fill.contract.contract_id,
                        dataset=lifecycle_datasets.get(
                            lifecycle.observation_dataset_hash
                        ),
                    )
                    lifecycle_events.append(
                        simulate_option_lifecycle(
                            position,
                            event_id=lifecycle.event_id,
                            event_at=lifecycle.event_at,
                            settlement_input=lifecycle.settlement_input,
                            exercise_fraction=lifecycle.exercise_fraction,
                            early_exercise_decision=lifecycle.early_exercise_decision,
                        )
                    )
                    lifecycle_observation_hashes.append(
                        lifecycle.observation_dataset_hash
                    )
                positions.append(position)
                inputs.append(valuation)
                ivs.append(iv)
                greeks.append(greek)
                marks.append(mark)
        except DerivativeResearchError as exc:
            raise self._failure_from_option(request, exc) from exc

        simulation = DerivativeSimulationEvidence.from_multi_leg(
            simulation_id=request.simulation_id,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            chain=request.chain,
            execution_policy=request.execution_policy,
            valuation_model=request.valuation_model,
            order=request.order,
            execution_result=result,
            positions=positions,
            valuation_inputs=inputs,
            implied_volatilities=ivs,
            greeks=greeks,
            marks=marks,
            lifecycle_events=lifecycle_events,
            lifecycle_datasets=request.lifecycle_datasets,
            lifecycle_observation_dataset_hashes=lifecycle_observation_hashes,
            participation_rates=request.participation_rates,
        )
        return self._success(
            run_id=request.run_id,
            preregistration=request.preregistration,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            simulation=simulation,
            started_at=request.started_at,
            finished_at=request.finished_at,
            observation_dataset_snapshot_hashes=tuple(
                item.content_hash for item in request.lifecycle_datasets
            ),
        )

    @staticmethod
    def _validate_valuation_input(
        value: ValuationInputSnapshot,
        contract_hash: str,
        quote_hash: str,
        dataset: DerivativeDatasetSnapshot,
    ) -> None:
        if value.contract.content_hash != contract_hash:
            raise DerivativeResearchError("option_valuation_contract_not_in_chain")
        if value.quote.content_hash != quote_hash:
            raise DerivativeResearchError("option_valuation_quote_not_in_chain")
        if not set(value.source_manifest_hashes).issubset(dataset.raw_manifest_hashes):
            raise DerivativeResearchError("option_valuation_source_not_in_dataset")
        valuation_at = parse_timestamp(
            value.valuation_at, "option_valuation_input.valuation_at"
        )
        if not (
            parse_timestamp(dataset.period_start, "derivative_dataset.period_start")
            <= valuation_at
            <= parse_timestamp(dataset.period_end, "derivative_dataset.period_end")
        ):
            raise DerivativeResearchError("option_valuation_outside_dataset_period")

    @staticmethod
    def _validate_settlement_input(
        value: OptionSettlementInput,
        *,
        contract_id: str,
        dataset: DerivativeDatasetSnapshot | None,
    ) -> None:
        if dataset is None:
            raise DerivativeResearchError(
                "option_settlement_observation_dataset_required"
            )
        if dataset.instrument_kind is not InstrumentKind.OPTION:
            raise DerivativeResearchError(
                "option_settlement_observation_dataset_instrument_mismatch"
            )
        if value.contract_id != contract_id:
            raise DerivativeResearchError(
                "option_settlement_input_contract_not_in_chain"
            )
        if value.source_manifest_hash not in dataset.raw_manifest_hashes:
            raise DerivativeResearchError(
                "option_settlement_input_source_not_in_observation_dataset"
            )
        if contract_id not in dataset.universe_ids:
            raise DerivativeResearchError(
                "option_settlement_contract_not_in_observation_dataset"
            )
        period_start = parse_timestamp(
            dataset.period_start, "option_settlement_dataset.period_start"
        )
        period_end = parse_timestamp(
            dataset.period_end, "option_settlement_dataset.period_end"
        )
        observation_times = tuple(
            parse_timestamp(
                getattr(value.availability, field_name),
                f"option_settlement_input.{field_name}",
            )
            for field_name in (
                "event_at",
                "published_at",
                "provider_received_at",
                "system_received_at",
                "processed_at",
            )
        )
        settlement_at = parse_timestamp(
            value.settlement_at, "option_settlement_input.settlement_at"
        )
        if any(
            instant < period_start or instant > period_end
            for instant in (*observation_times, settlement_at)
        ):
            raise DerivativeResearchError(
                "option_settlement_input_outside_observation_dataset_period"
            )
        if observation_times[-1] > parse_timestamp(
            dataset.knowledge_time, "option_settlement_dataset.knowledge_time"
        ):
            raise DerivativeResearchError(
                "option_settlement_input_after_observation_dataset_knowledge"
            )

    def _admit_option(self, request: OptionStudyRequest | MultiLegStudyRequest) -> None:
        executed_contract_ids = (
            tuple(item.contract_id for item in request.orders)
            if isinstance(request, OptionStudyRequest)
            else tuple(item.contract.contract_id for item in request.order.legs)
        )
        self._admit_common(
            preregistration=request.preregistration,
            instrument_kind=InstrumentKind.OPTION,
            dataset=request.dataset,
            experiment_spec=request.experiment_spec,
            chain_hash=request.chain.content_hash,
            chain_knowledge_time=request.chain.knowledge_time,
            chain_source_manifest_hashes=request.chain.source_manifest_hashes,
            market_ids=tuple(
                sorted({item.exchange for item in request.chain.contracts})
            ),
            target_ids=(
                request.chain.underlying_id,
                *executed_contract_ids,
            ),
            contract_ids=tuple(item.contract_id for item in request.chain.contracts),
            dataset_kinds=("point_in_time_chain", "option_chain"),
            first_access_at=request.started_at,
        )
        request.chain.admit(request.experiment_spec.run_type)
        lifecycle_references = (
            tuple(
                (item.lifecycle.observation_dataset_hash, item.lifecycle.event_at)
                for item in request.orders
                if item.lifecycle is not None
            )
            if isinstance(request, OptionStudyRequest)
            else tuple(
                (item.observation_dataset_hash, item.event_at)
                for _contract_id, item in request.lifecycle_by_contract
            )
        )
        lifecycle_datasets = {
            item.content_hash: item for item in request.lifecycle_datasets
        }
        frozen_at = parse_timestamp(
            request.experiment_spec.frozen_at, "option_study.spec_frozen_at"
        )
        finished_at = parse_timestamp(request.finished_at, "option_study.finished_at")
        for dataset_hash, event_at in lifecycle_references:
            lifecycle_dataset = lifecycle_datasets[dataset_hash]
            if lifecycle_dataset.content_hash == request.dataset.content_hash:
                raise DerivativeResearchError(
                    "option_lifecycle_dataset_must_be_separate"
                )
            if lifecycle_dataset.instrument_kind is not InstrumentKind.OPTION:
                raise DerivativeResearchError(
                    "option_lifecycle_dataset_instrument_mismatch"
                )
            knowledge_time = parse_timestamp(
                lifecycle_dataset.knowledge_time,
                "option_lifecycle_dataset.knowledge_time",
            )
            lifecycle_event_at = parse_timestamp(event_at, "option_lifecycle.event_at")
            if not (frozen_at <= lifecycle_event_at <= knowledge_time <= finished_at):
                raise DerivativeResearchError(
                    "option_lifecycle_dataset_chronology_invalid"
                )
            lifecycle_dataset.admit(request.experiment_spec.run_type)
        if (
            request.experiment_spec.simulation_policy_hash
            != request.execution_policy.content_hash
        ):
            raise DerivativeResearchError("option_study_execution_policy_mismatch")
        if (
            request.experiment_spec.cost_model_hash
            != request.execution_policy.cost_model_hash
        ):
            raise DerivativeResearchError("option_study_cost_model_mismatch")
        if (
            request.experiment_spec.fill_model_hash
            != request.execution_policy.fill_model_hash
        ):
            raise DerivativeResearchError("option_study_fill_model_mismatch")
        if (
            request.experiment_spec.valuation_model_hash
            != request.valuation_model.content_hash
        ):
            raise DerivativeResearchError("option_study_valuation_model_mismatch")

    @staticmethod
    def _admit_common(
        *,
        preregistration: ResearchPreregistration,
        instrument_kind: InstrumentKind,
        dataset: DerivativeDatasetSnapshot,
        experiment_spec: DerivativeExperimentSpec,
        chain_hash: str,
        chain_knowledge_time: str,
        chain_source_manifest_hashes: tuple[str, ...],
        market_ids: tuple[str, ...],
        target_ids: tuple[str, ...],
        contract_ids: tuple[str, ...],
        dataset_kinds: tuple[str, ...],
        first_access_at: str,
    ) -> None:
        if dataset.instrument_kind is not instrument_kind:
            raise DerivativeResearchError("derivative_study_instrument_mismatch")
        if experiment_spec.dataset_snapshot_hash != dataset.content_hash:
            raise DerivativeResearchError("derivative_study_dataset_mismatch")
        if experiment_spec.feature_version_hashes != dataset.feature_definition_hashes:
            raise DerivativeResearchError("derivative_study_feature_version_mismatch")
        if chain_hash not in dataset.chain_snapshot_hashes:
            raise DerivativeResearchError("derivative_study_chain_mismatch")
        if not set(chain_source_manifest_hashes).issubset(dataset.raw_manifest_hashes):
            raise DerivativeResearchError("derivative_study_chain_source_unbound")
        first_access = parse_timestamp(
            first_access_at, "derivative_study.first_access_at"
        )
        if (
            parse_timestamp(
                experiment_spec.frozen_at, "derivative_study.experiment_frozen_at"
            )
            > first_access
        ):
            raise DerivativeResearchError("derivative_study_frozen_after_access")
        if (
            parse_timestamp(
                dataset.knowledge_time, "derivative_study.dataset_knowledge_time"
            )
            > first_access
        ):
            raise DerivativeResearchError("derivative_study_future_dataset_knowledge")
        if (
            parse_timestamp(
                chain_knowledge_time, "derivative_study.chain_knowledge_time"
            )
            > first_access
        ):
            raise DerivativeResearchError("derivative_study_future_chain_knowledge")
        chain_time = parse_timestamp(
            chain_knowledge_time, "derivative_study.chain_knowledge_time"
        )
        if not (
            parse_timestamp(dataset.period_start, "derivative_dataset.period_start")
            <= chain_time
            <= parse_timestamp(dataset.period_end, "derivative_dataset.period_end")
        ):
            raise DerivativeResearchError("derivative_study_chain_outside_period")
        if set(dataset.universe_ids) != set(contract_ids):
            raise DerivativeResearchError("derivative_study_universe_chain_mismatch")
        preregistration.admit(
            instrument_kind=instrument_kind,
            experiment_spec=experiment_spec,
            first_dataset_access_at=first_access_at,
            market_ids=market_ids,
            target_ids=target_ids,
            dataset_kinds=dataset_kinds,
        )
        dataset.admit(experiment_spec.run_type)

    @staticmethod
    def _success(
        *,
        run_id: str,
        preregistration: ResearchPreregistration,
        dataset: DerivativeDatasetSnapshot,
        experiment_spec: DerivativeExperimentSpec,
        simulation: DerivativeSimulationEvidence,
        started_at: str,
        finished_at: str,
        observation_dataset_snapshot_hashes: tuple[str, ...] = (),
    ) -> DerivativeStudyExecution:
        run = DerivativeExperimentRun(
            run_id=run_id,
            experiment_spec_hash=experiment_spec.content_hash,
            dataset_snapshot_hash=dataset.content_hash,
            started_at=started_at,
            finished_at=finished_at,
            status="SUCCEEDED",
            event_stream_hash=simulation.event_stream_hash,
            result_artifact_hash=simulation.content_hash,
            observation_dataset_snapshot_hashes=observation_dataset_snapshot_hashes,
        )
        return DerivativeStudyExecution(
            preregistration_hash=preregistration.content_hash,
            simulation=simulation,
            run=run,
        )

    @staticmethod
    def _reproduce(
        *,
        request_hash: str,
        expected: DerivativeStudyExecution,
        reproduced: DerivativeStudyExecution,
        reproduction_id: str,
        verified_at: str,
    ) -> DerivativeReproductionReceipt:
        if parse_timestamp(verified_at, "reproduction.verified_at") < parse_timestamp(
            expected.run.finished_at, "reproduction.expected_run.finished_at"
        ):
            raise DerivativeResearchError("reproduction_before_run_finished")
        mismatch: list[str] = []
        if expected.preregistration_hash != reproduced.preregistration_hash:
            mismatch.append("preregistration_hash")
        if expected.simulation.content_hash != reproduced.simulation.content_hash:
            mismatch.append("simulation_hash")
        if (
            expected.simulation.event_stream_hash
            != reproduced.simulation.event_stream_hash
        ):
            mismatch.append("event_stream_hash")
        if expected.run.content_hash != reproduced.run.content_hash:
            mismatch.append("run_hash")
        return DerivativeReproductionReceipt(
            reproduction_id=reproduction_id,
            request_hash=request_hash,
            expected_run_hash=expected.run.content_hash,
            expected_simulation_hash=expected.simulation.content_hash,
            reproduced_run_hash=reproduced.run.content_hash,
            reproduced_simulation_hash=reproduced.simulation.content_hash,
            reproduced_failure_result_hash=None,
            verified_at=verified_at,
            status=(ReproductionStatus.FAIL if mismatch else ReproductionStatus.PASS),
            mismatch_fields=tuple(mismatch),
        )

    @staticmethod
    def _failed_reproduction(
        *,
        request_hash: str,
        expected: DerivativeStudyExecution,
        failure: DerivativeApplicationError,
        reproduction_id: str,
        verified_at: str,
    ) -> DerivativeReproductionReceipt:
        if parse_timestamp(verified_at, "reproduction.verified_at") < parse_timestamp(
            expected.run.finished_at, "reproduction.expected_run.finished_at"
        ):
            raise DerivativeResearchError("reproduction_before_run_finished")
        return DerivativeReproductionReceipt(
            reproduction_id=reproduction_id,
            request_hash=request_hash,
            expected_run_hash=expected.run.content_hash,
            expected_simulation_hash=expected.simulation.content_hash,
            reproduced_run_hash=failure.failed_run.content_hash,
            reproduced_simulation_hash=None,
            reproduced_failure_result_hash=failure.failure_result.content_hash,
            verified_at=verified_at,
            status=ReproductionStatus.FAIL,
            mismatch_fields=("reproduced_run_failed",),
        )

    @staticmethod
    def _failure_from_option(
        request: OptionStudyRequest | MultiLegStudyRequest,
        exc: DerivativeResearchError,
    ) -> DerivativeApplicationError:
        return DerivativeResearchApplicationService._failure(
            request.run_id,
            request.experiment_spec,
            request.dataset,
            request.started_at,
            request.finished_at,
            exc,
            observation_dataset_snapshot_hashes=tuple(
                item.content_hash for item in request.lifecycle_datasets
            ),
        )

    @staticmethod
    def _failure(
        run_id: str,
        experiment_spec: DerivativeExperimentSpec,
        dataset: DerivativeDatasetSnapshot,
        started_at: str,
        finished_at: str,
        exc: DerivativeResearchError,
        observation_dataset_snapshot_hashes: tuple[str, ...] = (),
    ) -> DerivativeApplicationError:
        raw_code = str(exc).split(":", 1)[0] or type(exc).__name__
        failure_code = _FAILURE_CODE.sub("_", raw_code).strip("_")
        failure_code = failure_code or "derivative_simulation_failed"
        event_hash = sha256_prefixed(
            {
                "run_id": run_id,
                "failure_code": failure_code,
                "experiment_spec_hash": experiment_spec.content_hash,
                "dataset_snapshot_hash": dataset.content_hash,
            },
            label="derivative_failed_event_stream",
        )
        failure_result = DerivativeFailureResult(
            run_id=run_id,
            event_stream_hash=event_hash,
            failure_code=failure_code,
            message_sha256=sha256_prefixed(
                {"message": str(exc)},
                label="derivative_application_failure_message",
            ),
        )
        run = DerivativeExperimentRun(
            run_id=run_id,
            experiment_spec_hash=experiment_spec.content_hash,
            dataset_snapshot_hash=dataset.content_hash,
            started_at=started_at,
            finished_at=finished_at,
            status="FAILED",
            event_stream_hash=event_hash,
            result_artifact_hash=failure_result.content_hash,
            failure_code=failure_code,
            observation_dataset_snapshot_hashes=observation_dataset_snapshot_hashes,
        )
        return DerivativeApplicationError(
            str(exc), failed_run=run, failure_result=failure_result
        )


def _research_context_payload(value: ResearchPreregistration) -> dict[str, object]:
    return {
        "content_hash": value.content_hash,
        "observation_hashes": [item.content_hash for item in value.observations],
        "research_question_hash": value.research_question.content_hash,
        "hypothesis_version_hash": value.hypothesis_version.content_hash,
        "preregistration_transition_hash": value.transition.content_hash,
    }


def _futures_command_payload(command: FuturesCommand) -> dict[str, object]:
    if isinstance(command, FuturesOrderCommand):
        return {
            "kind": "ORDER",
            "intent": command.intent.as_dict(),
            "fill_id": command.fill_id,
            "step_id": command.step_id,
        }
    if isinstance(command, FuturesSettlementCommand):
        return {
            "kind": "SETTLEMENT",
            "contract_id": command.contract_id,
            "as_of": command.as_of,
            "event_id": command.event_id,
            "step_id": command.step_id,
        }
    if isinstance(command, FuturesRollCommand):
        return {
            "kind": "ROLL",
            "decision": command.decision.as_dict(),
            "execution_id": command.execution_id,
            "step_id": command.step_id,
        }
    if isinstance(command, FuturesExpirationCommand):
        return {
            "kind": "EXPIRATION",
            "contract_id": command.contract_id,
            "as_of": command.as_of,
            "event_id": command.event_id,
            "step_id": command.step_id,
        }
    return {
        "kind": "SPREAD",
        "order": command.order.as_dict(),
        "execution_id": command.execution_id,
        "step_id": command.step_id,
    }


def _futures_request_hash(request: FuturesStudyRequest) -> str:
    return sha256_prefixed(
        {
            "run_id": request.run_id,
            "simulation_id": request.simulation_id,
            "ledger_id": request.ledger_id,
            "started_at": request.started_at,
            "finished_at": request.finished_at,
            "initial_cash": decimal_text(request.initial_cash),
            "preregistration": _research_context_payload(request.preregistration),
            "dataset_hash": request.dataset.content_hash,
            "experiment_spec_hash": request.experiment_spec.content_hash,
            "chain_hash": request.chain.content_hash,
            "simulator_hash": request.simulator.content_hash,
            "commands": [_futures_command_payload(item) for item in request.commands],
            "lifecycle_event_hashes": [
                item.content_hash for item in request.lifecycle_events
            ],
        },
        label="futures_study_request",
    )


def _early_exercise_payload(
    value: EarlyExerciseDecision | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "contract_id": value.contract_id,
        "evaluated_at": value.evaluated_at,
        "permitted": value.permitted,
        "exercise": value.exercise,
        "intrinsic_value": decimal_text(value.intrinsic_value),
        "continuation_value": decimal_text(value.continuation_value),
        "transaction_cost": decimal_text(value.transaction_cost),
        "reason": value.reason,
    }


def _lifecycle_command_payload(
    value: OptionLifecycleCommand | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "event_id": value.event_id,
        "event_at": value.event_at,
        "observation_dataset_hash": value.observation_dataset_hash,
        "settlement_input": value.settlement_input.as_dict(),
        "exercise_fraction": decimal_text(value.exercise_fraction),
        "early_exercise_decision": _early_exercise_payload(
            value.early_exercise_decision
        ),
    }


def _option_model_payload(value: BlackScholesModel) -> dict[str, object]:
    return {
        "model_version": value.model_version,
        "minimum_volatility": decimal_text(value.minimum_volatility),
        "maximum_volatility": decimal_text(value.maximum_volatility),
        "price_tolerance": decimal_text(value.price_tolerance),
        "maximum_iterations": value.maximum_iterations,
    }


def _option_request_hash(request: OptionStudyRequest) -> str:
    return sha256_prefixed(
        {
            "run_id": request.run_id,
            "simulation_id": request.simulation_id,
            "started_at": request.started_at,
            "finished_at": request.finished_at,
            "preregistration": _research_context_payload(request.preregistration),
            "dataset_hash": request.dataset.content_hash,
            "lifecycle_dataset_hashes": [
                item.content_hash for item in request.lifecycle_datasets
            ],
            "experiment_spec_hash": request.experiment_spec.content_hash,
            "chain_hash": request.chain.content_hash,
            "execution_policy_hash": request.execution_policy.content_hash,
            "valuation_model": _option_model_payload(request.valuation_model),
            "orders": [
                {
                    "order_id": item.order_id,
                    "position_id": item.position_id,
                    "contract_id": item.contract_id,
                    "side": item.side.value,
                    "quantity": decimal_text(item.quantity),
                    "requested_at": item.requested_at,
                    "valuation_input_hash": item.valuation_input.content_hash,
                    "participation_rate": decimal_text(item.participation_rate),
                    "lifecycle": _lifecycle_command_payload(item.lifecycle),
                }
                for item in request.orders
            ],
        },
        label="option_study_request",
    )


def _multileg_request_hash(request: MultiLegStudyRequest) -> str:
    return sha256_prefixed(
        {
            "run_id": request.run_id,
            "simulation_id": request.simulation_id,
            "started_at": request.started_at,
            "finished_at": request.finished_at,
            "preregistration": _research_context_payload(request.preregistration),
            "dataset_hash": request.dataset.content_hash,
            "lifecycle_dataset_hashes": [
                item.content_hash for item in request.lifecycle_datasets
            ],
            "experiment_spec_hash": request.experiment_spec.content_hash,
            "chain_hash": request.chain.content_hash,
            "execution_policy_hash": request.execution_policy.content_hash,
            "valuation_model": _option_model_payload(request.valuation_model),
            "order": request.order.identity_payload(),
            "valuation_input_hashes": [
                item.content_hash for item in request.valuation_inputs
            ],
            "fill_times": dict(request.fill_times),
            "participation_rates": {
                key: decimal_text(value) for key, value in request.participation_rates
            },
            "lifecycle_by_contract": {
                key: _lifecycle_command_payload(value)
                for key, value in request.lifecycle_by_contract
            },
        },
        label="multileg_study_request",
    )
