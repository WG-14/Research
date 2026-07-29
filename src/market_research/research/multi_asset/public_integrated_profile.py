"""Source-owned public institutional T-04 conformance orchestration.

This module is intentionally independent from the builtin runner.  It accepts
immutable hypotheses, contracts, quotes, policies, and source observations,
then creates every decision and receipt itself.  No caller-selected quantity,
execution result, ledger receipt, or reconciliation receipt is accepted.
"""

from __future__ import annotations

from dataclasses import InitVar, asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
import re
from typing import Mapping, Sequence, cast

from market_research.research.derivatives.common import AvailabilityTimes
from market_research.research.derivatives.options import (
    ExerciseStyle,
    MultiLegExecutionPolicy,
    OptionContract,
    OptionQuote,
    OptionType,
    SettlementType as OptionSettlementType,
    ValuationInputSnapshot,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.instrument_kinds import InstrumentKind
from market_research.research.multi_asset.accounting import (
    AccountingReconciliationError,
    AdvancedAccountingReconciliation,
    LedgerPnlReconciliation,
    PitFxObservation,
    ReportLedgerReconciliation,
    ReportPnlSummary,
    encode_report_payload,
    report_payload_hash,
)
from market_research.research.multi_asset.domain import (
    ContractSpecification,
    EconomicUnderlying,
    EffectivePeriod,
    Instrument,
    InstrumentRegistry,
    InstrumentRelationship,
    InstrumentRelationshipType,
    SettlementType as DomainSettlementType,
    SourceReference,
)
from market_research.research.multi_asset.exposure import (
    ExposureEngine,
    ExposureEngineError,
    ExtendedPortfolioExposure,
    OffsetPolicyV3,
    OptionValuationAdapter,
    PortfolioExposureSnapshot,
    PositionRiskSupplement,
    build_extended_portfolio_exposure,
)
from market_research.research.multi_asset.expression import (
    DEFAULT_EXPRESSION_POLICY,
    DeterministicJointOptimizer,
    DesiredEconomicPayoff,
    Direction,
    EconomicHypothesis,
    ExecutionMode,
    ExpectedMarketDistribution,
    ExpressionCandidate,
    ExpressionDecision,
    ExpressionKind,
    ExpressionPolicy,
    ExpressionValidationError,
    InstrumentChoice,
    InstrumentExpressionEngine,
    JointOptimizationConstraints,
    JointOptimizationProblem,
    LegRole,
    LegSelectionRule,
    OptimizationLeg,
    ProductKind,
    ScenarioRange,
    StrategyTargets,
)
from market_research.research.multi_asset.market_state import (
    FXQuote,
    LiquidityQuote,
    MarketState,
    ObservationMetadata,
    OptionChainState,
    OptionContractQuote,
    OptionRight,
    QuoteCondition,
    SpotQuote,
)
from market_research.research.multi_asset.multileg_execution import (
    DynamicExecutionAttempt,
    DynamicExecutionPolicy,
    DynamicMultiLegExecutionPlan,
    DynamicMultiLegExecutionResult,
    DynamicMultiLegExecutionService,
    DynamicTimeInForce,
    ExpressionCompilePolicy,
    ExpressionExecutionCompiler,
    InterLegMarketMove,
    LedgerExposureBinding,
    LedgerExposureRequest,
    LifecycleActionType,
    LifecycleTransitionEvidence,
    MultiLegDisposition,
    MultiLegLedgerError,
    MultiLegLedgerExecutionService,
    MultiLegLifecycleEvidenceChain,
    SequentialPartialAction,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    CollateralAssetBalance,
    CollateralWaterfallPolicy,
    ExternalFlowConversionEvidence,
    FundingFxRevaluation,
    InvariantAccountingFactory,
    PortfolioAccountingError,
    PortfolioEventType,
    TaxLotMethod,
    UnifiedPortfolioLedger,
    allocate_collateral_waterfall,
    funding_event,
    project_tax_lots,
    publish_advanced_accounting_bundle,
    revalue_funding_fx,
    trade_event,
)
from market_research.research.multi_asset.option_analytics import (
    AuthoritativeOptionAnalyticsFactory,
    default_analytics_comparison_policy,
    default_option_model_registry,
)
from market_research.research.multi_asset.scenarios import (
    ConstrainedJointScenarioEngine,
    EconomicProjectionPolicy,
    PathRiskLimits,
    PathScenarioEngine,
    PathScenarioResult,
    ScenarioError,
    ScenarioFactorObservation,
    ScenarioPathFactory,
    ScenarioPathGenerationSpec,
    ScenarioPathMode,
)


PUBLIC_T04_SCHEMA_VERSION = 1
MAX_T04_CANDIDATES = 8
MAX_T04_LEGS = 6
MAX_T04_EXECUTION_ATTEMPTS = 4
MAX_T04_LIFECYCLE_ACTIONS = 16
MAX_T04_STRESS_STEPS = 16
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_RECEIPT_TOKEN = object()


class PublicIntegratedProfileError(ValueError):
    """The public institutional profile cannot establish T-04 conformance."""


class PublicProfileProvenanceKind(StrEnum):
    EXTERNALLY_PREPARED_IMMUTABLE = "EXTERNALLY_PREPARED_IMMUTABLE"
    EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE = (
        "EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE"
    )


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PublicIntegratedProfileError(f"{label}_invalid")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise PublicIntegratedProfileError(f"{label}_invalid_hash")
    return value


def _decimal(
    value: object,
    label: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
    ):
        raise PublicIntegratedProfileError(f"{label}_finite_decimal_required")
    if positive and value <= _ZERO:
        raise PublicIntegratedProfileError(f"{label}_positive_required")
    if nonnegative and value < _ZERO:
        raise PublicIntegratedProfileError(f"{label}_nonnegative_required")
    return value


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PublicIntegratedProfileError(f"{label}_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicIntegratedProfileError(f"{label}_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicIntegratedProfileError(f"{label}_timezone_required")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _pairs(
    values: Sequence[tuple[str, Decimal]],
    label: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> tuple[tuple[str, Decimal], ...]:
    result = tuple(values)
    if result != tuple(sorted(result)) or len({key for key, _ in result}) != len(
        result
    ):
        raise PublicIntegratedProfileError(f"{label}_not_sorted_unique")
    for key, value in result:
        _require_id(key, f"{label}.key")
        _decimal(
            value,
            f"{label}.{key}",
            positive=positive,
            nonnegative=nonnegative,
        )
    return result


def _component_hash(value: object, *, label: str) -> str:
    return sha256_prefixed(_hash_json_value(value), label=label)


def _hash_json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return _timestamp_text(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _hash_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hash_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PublicT04Provenance:
    source_document_id: str
    kind: PublicProfileProvenanceKind
    source_document_hashes: tuple[str, ...]
    quality_flags: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.source_document_id, "public_t04_provenance.document_id")
        if not isinstance(self.kind, PublicProfileProvenanceKind):
            raise PublicIntegratedProfileError("public_t04_provenance_kind_invalid")
        if not self.source_document_hashes or self.source_document_hashes != tuple(
            sorted(set(self.source_document_hashes))
        ):
            raise PublicIntegratedProfileError(
                "public_t04_provenance_source_hashes_invalid"
            )
        for value in self.source_document_hashes:
            _require_hash(value, "public_t04_provenance.source_hash")
        if not self.quality_flags or self.quality_flags != tuple(
            sorted(set(self.quality_flags))
        ):
            raise PublicIntegratedProfileError(
                "public_t04_provenance_quality_flags_invalid"
            )
        for value in self.quality_flags:
            if not re.fullmatch(r"[A-Z][A-Z0-9_:-]{0,127}", value):
                raise PublicIntegratedProfileError(
                    "public_t04_provenance_quality_flag_invalid"
                )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "source_document_id": self.source_document_id,
                    "kind": self.kind.value,
                    "source_document_hashes": list(self.source_document_hashes),
                    "quality_flags": list(self.quality_flags),
                },
                label="public_t04_provenance",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicInterLegMoveInput:
    instrument_id: str
    before_price: Decimal
    after_price: Decimal
    before_state_hash: str
    after_state_hash: str
    exposure_before_hash: str
    exposure_after_hash: str
    cost_before_hash: str
    cost_after_hash: str
    margin_before_hash: str
    margin_after_hash: str

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "public_t04_interleg.instrument_id")
        _decimal(self.before_price, "public_t04_interleg.before_price", positive=True)
        _decimal(self.after_price, "public_t04_interleg.after_price", positive=True)
        for name in (
            "before_state_hash",
            "after_state_hash",
            "exposure_before_hash",
            "exposure_after_hash",
            "cost_before_hash",
            "cost_after_hash",
            "margin_before_hash",
            "margin_after_hash",
        ):
            _require_hash(getattr(self, name), f"public_t04_interleg.{name}")


@dataclass(frozen=True, slots=True)
class PublicExecutionAttempt:
    execution_id: str
    quotes: tuple[OptionQuote, ...]
    fill_times: tuple[tuple[str, str], ...]
    participation_rates: tuple[tuple[str, Decimal], ...]
    market_state_hash: str
    quote_state_hash: str
    unwind_at: str
    interleg_moves: tuple[PublicInterLegMoveInput, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.execution_id, "public_t04_attempt.execution_id")
        quote_ids = tuple(item.contract_id for item in self.quotes)
        if not quote_ids or quote_ids != tuple(sorted(set(quote_ids))):
            raise PublicIntegratedProfileError(
                "public_t04_attempt_quotes_not_sorted_unique"
            )
        fill_times = tuple(self.fill_times)
        if fill_times != tuple(sorted(fill_times)) or {
            key for key, _ in fill_times
        } != set(quote_ids):
            raise PublicIntegratedProfileError(
                "public_t04_attempt_fill_time_coverage_invalid"
            )
        for instrument_id, value in fill_times:
            _require_id(instrument_id, "public_t04_attempt.fill_instrument")
            _timestamp(value, "public_t04_attempt.fill_time")
        participation = _pairs(
            self.participation_rates,
            "public_t04_attempt.participation",
            positive=True,
        )
        if not {key for key, _ in participation}.issubset(quote_ids):
            raise PublicIntegratedProfileError(
                "public_t04_attempt_participation_unknown"
            )
        _require_hash(
            self.market_state_hash,
            "public_t04_attempt.market_state",
        )
        _require_hash(
            self.quote_state_hash,
            "public_t04_attempt.quote_state",
        )
        unwind = _timestamp(self.unwind_at, "public_t04_attempt.unwind_at")
        if unwind <= max(
            _timestamp(value, "public_t04_attempt.fill_time") for _, value in fill_times
        ):
            raise PublicIntegratedProfileError(
                "public_t04_attempt_unwind_not_after_fills"
            )
        moves = tuple(self.interleg_moves)
        if moves != tuple(sorted(moves, key=lambda item: item.instrument_id)):
            raise PublicIntegratedProfileError(
                "public_t04_attempt_interleg_moves_not_sorted"
            )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "execution_id": self.execution_id,
                    "quote_hashes": [item.content_hash for item in self.quotes],
                    "fill_times": list(fill_times),
                    "participation_rates": [
                        (key, _decimal_text(value)) for key, value in participation
                    ],
                    "market_state_hash": self.market_state_hash,
                    "quote_state_hash": self.quote_state_hash,
                    "unwind_at": self.unwind_at,
                    "interleg_moves": [asdict(item) for item in self.interleg_moves],
                },
                label="public_t04_execution_attempt_input",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicFundingInput:
    funding_id: str
    occurred_at: str
    currency: str
    amount: Decimal
    source_hash: str
    conversion_rate: Decimal | None = None
    conversion_source_hash: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.funding_id, "public_t04_funding.id")
        _timestamp(self.occurred_at, "public_t04_funding.occurred_at")
        if not _CURRENCY.fullmatch(self.currency):
            raise PublicIntegratedProfileError("public_t04_funding_currency_invalid")
        _decimal(self.amount, "public_t04_funding.amount")
        if self.amount == _ZERO:
            raise PublicIntegratedProfileError("public_t04_funding_zero_forbidden")
        _require_hash(self.source_hash, "public_t04_funding.source_hash")
        if (self.conversion_rate is None) != (self.conversion_source_hash is None):
            raise PublicIntegratedProfileError(
                "public_t04_funding_conversion_incomplete"
            )
        if self.conversion_rate is not None:
            _decimal(
                self.conversion_rate,
                "public_t04_funding.conversion_rate",
                positive=True,
            )
            _require_hash(
                self.conversion_source_hash,
                "public_t04_funding.conversion_source_hash",
            )


@dataclass(frozen=True, slots=True)
class PublicLifecycleRule:
    action: LifecycleActionType
    trigger_id: str
    occurred_at: str
    source_instrument_id: str
    execution_price: Decimal
    fraction: Decimal
    source_hash: str
    target_instrument_id: str | None = None
    target_price: Decimal | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.action not in {
            LifecycleActionType.HEDGE,
            LifecycleActionType.REBALANCE,
            LifecycleActionType.ROLL_EXPIRY,
            LifecycleActionType.ROLL_STRIKE,
            LifecycleActionType.PARTIAL_UNWIND,
            LifecycleActionType.FULL_UNWIND,
        }:
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_action_unsupported"
            )
        _require_id(self.trigger_id, "public_t04_lifecycle.trigger_id")
        _timestamp(self.occurred_at, "public_t04_lifecycle.occurred_at")
        _require_id(
            self.source_instrument_id,
            "public_t04_lifecycle.source_instrument_id",
        )
        _decimal(
            self.execution_price,
            "public_t04_lifecycle.execution_price",
            positive=True,
        )
        _decimal(
            self.fraction,
            "public_t04_lifecycle.fraction",
            positive=True,
        )
        if self.fraction > _ONE:
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_fraction_above_one"
            )
        _require_hash(self.source_hash, "public_t04_lifecycle.source_hash")
        is_roll = self.action in {
            LifecycleActionType.ROLL_EXPIRY,
            LifecycleActionType.ROLL_STRIKE,
        }
        if is_roll != (
            self.target_instrument_id is not None and self.target_price is not None
        ):
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_roll_target_binding_invalid"
            )
        if self.target_instrument_id is not None:
            _require_id(
                self.target_instrument_id,
                "public_t04_lifecycle.target_instrument_id",
            )
            if self.target_instrument_id == self.source_instrument_id:
                raise PublicIntegratedProfileError(
                    "public_t04_lifecycle_roll_target_same"
                )
        if self.target_price is not None:
            _decimal(
                self.target_price,
                "public_t04_lifecycle.target_price",
                positive=True,
            )
        if self.action is LifecycleActionType.FULL_UNWIND and self.fraction != _ONE:
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_full_unwind_fraction_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "action": self.action.value,
                    "trigger_id": self.trigger_id,
                    "occurred_at": self.occurred_at,
                    "source_instrument_id": self.source_instrument_id,
                    "execution_price": _decimal_text(self.execution_price),
                    "fraction": _decimal_text(self.fraction),
                    "source_hash": self.source_hash,
                    "target_instrument_id": self.target_instrument_id,
                    "target_price": (
                        None
                        if self.target_price is None
                        else _decimal_text(self.target_price)
                    ),
                },
                label="public_t04_lifecycle_rule",
            ),
        )


@dataclass(frozen=True, slots=True)
class HigherOrderRiskPolicy:
    policy_id: str
    version: str
    beta_multiplier: Decimal
    duration_fraction: Decimal
    vanna_multiplier: Decimal
    volga_multiplier: Decimal
    charm_multiplier: Decimal
    source_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "public_t04_higher_order.policy_id")
        _require_id(self.version, "public_t04_higher_order.version")
        for name in (
            "beta_multiplier",
            "duration_fraction",
            "vanna_multiplier",
            "volga_multiplier",
            "charm_multiplier",
        ):
            _decimal(getattr(self, name), f"public_t04_higher_order.{name}")
        _require_hash(self.source_hash, "public_t04_higher_order.source_hash")
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "policy_id": self.policy_id,
                    "version": self.version,
                    "beta_multiplier": _decimal_text(self.beta_multiplier),
                    "duration_fraction": _decimal_text(self.duration_fraction),
                    "vanna_multiplier": _decimal_text(self.vanna_multiplier),
                    "volga_multiplier": _decimal_text(self.volga_multiplier),
                    "charm_multiplier": _decimal_text(self.charm_multiplier),
                    "source_hash": self.source_hash,
                },
                label="public_t04_higher_order_policy",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicT04ResearchInputs:
    hypothesis: EconomicHypothesis
    payoff: DesiredEconomicPayoff
    candidates: tuple[ExpressionCandidate, ...]
    optimization_legs: tuple[OptimizationLeg, ...]
    optimization_constraints: JointOptimizationConstraints
    expression_policy: ExpressionPolicy = DEFAULT_EXPRESSION_POLICY
    maximum_combinations: int = 10_000
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not self.candidates
            or len(self.candidates) > MAX_T04_CANDIDATES
            or len(self.optimization_legs) < 2
            or len(self.optimization_legs) > MAX_T04_LEGS
        ):
            raise PublicIntegratedProfileError("public_t04_research_bounds_invalid")
        if (
            isinstance(self.maximum_combinations, bool)
            or not isinstance(self.maximum_combinations, int)
            or self.maximum_combinations <= 0
            or self.maximum_combinations > 100_000
        ):
            raise PublicIntegratedProfileError(
                "public_t04_maximum_combinations_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "hypothesis_hash": self.hypothesis.content_hash,
                    "payoff": asdict(self.payoff),
                    "candidate_ids": [item.candidate_id for item in self.candidates],
                    "candidates": [asdict(item) for item in self.candidates],
                    "optimization_legs": [
                        asdict(item) for item in self.optimization_legs
                    ],
                    "optimization_constraints": asdict(self.optimization_constraints),
                    "expression_policy_hash": self.expression_policy.content_hash,
                    "maximum_combinations": self.maximum_combinations,
                },
                label="public_t04_research_inputs",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicT04ExecutionInputs:
    contracts: tuple[OptionContract, ...]
    compile_policy: ExpressionCompilePolicy
    dynamic_policy: DynamicExecutionPolicy
    attempts: tuple[PublicExecutionAttempt, ...]
    ledger_fx_rates: tuple[tuple[str, Decimal], ...]
    fee_per_contract: Decimal = _ZERO
    slippage_ticks: int = 0
    unwind_fee_per_contract: Decimal = _ZERO
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        contract_ids = tuple(item.contract_id for item in self.contracts)
        if (
            len(contract_ids) < 2
            or len(contract_ids) > MAX_T04_LEGS
            or contract_ids != tuple(sorted(set(contract_ids)))
        ):
            raise PublicIntegratedProfileError("public_t04_execution_contracts_invalid")
        if (
            len(self.attempts) < 2
            or len(self.attempts) > MAX_T04_EXECUTION_ATTEMPTS
            or len(self.attempts) > self.dynamic_policy.maximum_attempts
        ):
            raise PublicIntegratedProfileError(
                "public_t04_execution_attempt_bounds_invalid"
            )
        if (
            self.compile_policy.execution_policy
            is not MultiLegExecutionPolicy.SEQUENTIAL
        ):
            raise PublicIntegratedProfileError(
                "public_t04_execution_sequential_required"
            )
        if not self.compile_policy.allow_partial:
            raise PublicIntegratedProfileError(
                "public_t04_execution_partial_simulation_required"
            )
        if self.dynamic_policy.accept_partial:
            raise PublicIntegratedProfileError(
                "public_t04_execution_partial_acceptance_forbidden"
            )
        if self.compile_policy.sequential_partial_action is not (
            SequentialPartialAction.UNWIND
        ):
            raise PublicIntegratedProfileError(
                "public_t04_execution_partial_unwind_required"
            )
        _pairs(
            self.ledger_fx_rates,
            "public_t04_execution.ledger_fx_rates",
            positive=True,
        )
        _decimal(
            self.fee_per_contract,
            "public_t04_execution.fee_per_contract",
            nonnegative=True,
        )
        _decimal(
            self.unwind_fee_per_contract,
            "public_t04_execution.unwind_fee",
            nonnegative=True,
        )
        if (
            isinstance(self.slippage_ticks, bool)
            or not isinstance(self.slippage_ticks, int)
            or self.slippage_ticks < 0
        ):
            raise PublicIntegratedProfileError("public_t04_execution_slippage_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "contract_hashes": [item.content_hash for item in self.contracts],
                    "compile_policy_hash": self.compile_policy.content_hash,
                    "dynamic_policy_hash": self.dynamic_policy.content_hash,
                    "attempt_hashes": [item.content_hash for item in self.attempts],
                    "ledger_fx_rates": [
                        (key, _decimal_text(value))
                        for key, value in self.ledger_fx_rates
                    ],
                    "fee_per_contract": _decimal_text(self.fee_per_contract),
                    "slippage_ticks": self.slippage_ticks,
                    "unwind_fee_per_contract": _decimal_text(
                        self.unwind_fee_per_contract
                    ),
                },
                label="public_t04_execution_inputs",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicT04AccountingInputs:
    base_currency: str
    funding: tuple[PublicFundingInput, ...]
    lifecycle_rules: tuple[PublicLifecycleRule, ...]
    collateral_assets: tuple[CollateralAssetBalance, ...]
    collateral_policy: CollateralWaterfallPolicy
    required_collateral_base: Decimal
    collateral_at: str
    funding_fx_at: str
    current_fx_rates: tuple[tuple[str, Decimal], ...]
    current_fx_source_hash: str
    fx_observations: tuple[PitFxObservation, ...]
    tax_lot_method: TaxLotMethod = TaxLotMethod.FIFO
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not _CURRENCY.fullmatch(self.base_currency):
            raise PublicIntegratedProfileError(
                "public_t04_accounting_base_currency_invalid"
            )
        if not self.funding or not any(
            item.currency != self.base_currency for item in self.funding
        ):
            raise PublicIntegratedProfileError(
                "public_t04_accounting_multicurrency_funding_required"
            )
        funding_ids = [item.funding_id for item in self.funding]
        if len(funding_ids) != len(set(funding_ids)):
            raise PublicIntegratedProfileError(
                "public_t04_accounting_funding_duplicate"
            )
        for item in self.funding:
            if item.currency == self.base_currency:
                if item.conversion_rate is not None:
                    raise PublicIntegratedProfileError(
                        "public_t04_base_funding_conversion_forbidden"
                    )
            elif item.conversion_rate is None:
                raise PublicIntegratedProfileError(
                    "public_t04_nonbase_funding_conversion_required"
                )
        if (
            not self.lifecycle_rules
            or len(self.lifecycle_rules) > MAX_T04_LIFECYCLE_ACTIONS
        ):
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_rule_bounds_invalid"
            )
        action_set = {item.action for item in self.lifecycle_rules}
        required_actions = {
            LifecycleActionType.HEDGE,
            LifecycleActionType.REBALANCE,
            LifecycleActionType.ROLL_EXPIRY,
            LifecycleActionType.PARTIAL_UNWIND,
        }
        if not required_actions.issubset(action_set):
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_required_actions_missing"
            )
        times = [
            _timestamp(item.occurred_at, "public_t04_lifecycle.occurred_at")
            for item in self.lifecycle_rules
        ]
        if times != sorted(times) or len(times) != len(set(times)):
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_chronology_invalid"
            )
        _decimal(
            self.required_collateral_base,
            "public_t04_accounting.required_collateral",
            positive=True,
        )
        collateral_at = _timestamp(
            self.collateral_at,
            "public_t04_accounting.collateral_at",
        )
        funding_fx_at = _timestamp(
            self.funding_fx_at,
            "public_t04_accounting.funding_fx_at",
        )
        if collateral_at <= times[-1] or funding_fx_at <= collateral_at:
            raise PublicIntegratedProfileError(
                "public_t04_accounting_audit_chronology_invalid"
            )
        _pairs(
            self.current_fx_rates,
            "public_t04_accounting.current_fx_rates",
            positive=True,
        )
        _require_hash(
            self.current_fx_source_hash,
            "public_t04_accounting.current_fx_source_hash",
        )
        if not isinstance(self.tax_lot_method, TaxLotMethod):
            raise PublicIntegratedProfileError(
                "public_t04_accounting_tax_lot_method_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "base_currency": self.base_currency,
                    "funding": [asdict(item) for item in self.funding],
                    "lifecycle_rule_hashes": [
                        item.content_hash for item in self.lifecycle_rules
                    ],
                    "collateral_assets": [
                        asdict(item) for item in self.collateral_assets
                    ],
                    "collateral_policy_hash": self.collateral_policy.content_hash,
                    "required_collateral_base": _decimal_text(
                        self.required_collateral_base
                    ),
                    "collateral_at": self.collateral_at,
                    "funding_fx_at": self.funding_fx_at,
                    "current_fx_rates": [
                        (key, _decimal_text(value))
                        for key, value in self.current_fx_rates
                    ],
                    "current_fx_source_hash": self.current_fx_source_hash,
                    "fx_observations": [
                        item.content_hash for item in self.fx_observations
                    ],
                    "tax_lot_method": self.tax_lot_method.value,
                },
                label="public_t04_accounting_inputs",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicT04RiskInputs:
    exposure_request: LedgerExposureRequest
    higher_order_policy: HigherOrderRiskPolicy
    offset_policy: OffsetPolicyV3
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        request = self.exposure_request
        request_hash = _component_hash(
            {
                "snapshot_id": request.snapshot_id,
                "catalog_hash": request.engine.product_catalog.contract_hash(),
                "adapter_hashes": [
                    item.content_hash for item in request.engine.adapters
                ],
                "market_state_hash": request.market_state.state_hash(),
                "bindings": [item.as_dict() for item in request.bindings],
            },
            label="public_t04_exposure_request",
        )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "exposure_request_hash": request_hash,
                    "higher_order_policy_hash": (self.higher_order_policy.content_hash),
                    "offset_policy_hash": self.offset_policy.content_hash,
                },
                label="public_t04_risk_inputs",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicT04StressInputs:
    generation_spec: ScenarioPathGenerationSpec
    observations: tuple[ScenarioFactorObservation, ...]
    effective_times: tuple[str, ...]
    risk_limits: PathRiskLimits
    economic_policy: EconomicProjectionPolicy
    base_liquidation_costs: tuple[tuple[str, Decimal], ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.generation_spec.step_count > MAX_T04_STRESS_STEPS
            or len(self.effective_times) != self.generation_spec.step_count
            or not self.observations
        ):
            raise PublicIntegratedProfileError(
                "public_t04_stress_bounds_or_coverage_invalid"
            )
        times = [
            _timestamp(value, "public_t04_stress.effective_at")
            for value in self.effective_times
        ]
        if times != sorted(times) or len(times) != len(set(times)):
            raise PublicIntegratedProfileError(
                "public_t04_stress_effective_times_invalid"
            )
        _pairs(
            self.base_liquidation_costs,
            "public_t04_stress.liquidation_costs",
            nonnegative=True,
        )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "generation_spec_hash": self.generation_spec.content_hash,
                    "observation_hashes": [
                        item.content_hash for item in self.observations
                    ],
                    "effective_times": list(self.effective_times),
                    "risk_limits_hash": self.risk_limits.content_hash,
                    "economic_policy_hash": self.economic_policy.content_hash,
                    "base_liquidation_costs": [
                        (key, _decimal_text(value))
                        for key, value in self.base_liquidation_costs
                    ],
                },
                label="public_t04_stress_inputs",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicT04Inputs:
    profile_id: str
    opened_at: str
    closed_at: str
    provenance: PublicT04Provenance
    research: PublicT04ResearchInputs
    execution: PublicT04ExecutionInputs
    accounting: PublicT04AccountingInputs
    risk: PublicT04RiskInputs
    stress: PublicT04StressInputs
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.profile_id, "public_t04_inputs.profile_id")
        opened = _timestamp(self.opened_at, "public_t04_inputs.opened_at")
        closed = _timestamp(self.closed_at, "public_t04_inputs.closed_at")
        if opened >= closed:
            raise PublicIntegratedProfileError("public_t04_inputs_window_invalid")
        event_times = [
            *(
                _timestamp(item.occurred_at, "public_t04_inputs.funding_at")
                for item in self.accounting.funding
            ),
            *(
                _timestamp(item.occurred_at, "public_t04_inputs.lifecycle_at")
                for item in self.accounting.lifecycle_rules
            ),
            _timestamp(
                self.accounting.collateral_at,
                "public_t04_inputs.collateral_at",
            ),
            _timestamp(
                self.accounting.funding_fx_at,
                "public_t04_inputs.funding_fx_at",
            ),
        ]
        if any(item < opened or item > closed for item in event_times):
            raise PublicIntegratedProfileError("public_t04_inputs_event_outside_window")
        market_state = self.risk.exposure_request.market_state
        if (
            market_state.base_currency != self.accounting.base_currency
            or _timestamp(
                market_state.valuation_at,
                "public_t04_inputs.market_state_at",
            )
            <= max(event_times)
            or _timestamp(
                self.stress.effective_times[-1],
                "public_t04_inputs.last_stress_at",
            )
            > closed
        ):
            raise PublicIntegratedProfileError(
                "public_t04_inputs_market_or_stress_window_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                {
                    "schema_version": PUBLIC_T04_SCHEMA_VERSION,
                    "profile_id": self.profile_id,
                    "opened_at": self.opened_at,
                    "closed_at": self.closed_at,
                    "provenance_hash": self.provenance.content_hash,
                    "research_hash": self.research.content_hash,
                    "execution_hash": self.execution.content_hash,
                    "accounting_hash": self.accounting.content_hash,
                    "risk_hash": self.risk.content_hash,
                    "stress_hash": self.stress.content_hash,
                },
                label="public_t04_inputs",
            ),
        )


def build_public_t04_inputs(
    *,
    profile_id: str,
    opened_at: str,
    closed_at: str,
    provenance: PublicT04Provenance,
    research: PublicT04ResearchInputs,
    execution: PublicT04ExecutionInputs,
    accounting: PublicT04AccountingInputs,
    risk: PublicT04RiskInputs,
    stress: PublicT04StressInputs,
    caller_preselected_quantities: object | None = None,
    caller_receipts: Sequence[object] = (),
) -> PublicT04Inputs:
    """Build source-owned inputs while explicitly rejecting result injection."""

    if caller_preselected_quantities is not None:
        raise PublicIntegratedProfileError(
            "public_t04_caller_preselected_quantities_forbidden"
        )
    if tuple(caller_receipts):
        raise PublicIntegratedProfileError(
            "public_t04_caller_precomputed_receipts_forbidden"
        )
    return PublicT04Inputs(
        profile_id=profile_id,
        opened_at=opened_at,
        closed_at=closed_at,
        provenance=provenance,
        research=research,
        execution=execution,
        accounting=accounting,
        risk=risk,
        stress=stress,
    )


@dataclass(frozen=True, slots=True)
class PublicIntegratedProfileReceipt:
    profile_id: str
    inputs_hash: str
    provenance_hash: str
    selected_instruments: tuple[str, ...]
    selected_directions: tuple[tuple[str, str], ...]
    selected_quantities: tuple[tuple[str, int], ...]
    final_position_quantities: tuple[tuple[str, Decimal], ...]
    closing_ledger_hash: str
    report_pnl_rows: tuple[tuple[str, Decimal], ...]
    exposure_totals: tuple[tuple[str, Decimal], ...]
    component_hashes: tuple[tuple[str, str], ...]
    breaches: tuple[str, ...]
    actions: tuple[str, ...]
    quality_flags: tuple[str, ...]
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _RECEIPT_TOKEN:
            raise PublicIntegratedProfileError("public_t04_receipt_requires_factory")
        _require_id(self.profile_id, "public_t04_receipt.profile_id")
        _require_hash(self.inputs_hash, "public_t04_receipt.inputs_hash")
        _require_hash(
            self.provenance_hash,
            "public_t04_receipt.provenance_hash",
        )
        _require_hash(
            self.closing_ledger_hash,
            "public_t04_receipt.closing_ledger_hash",
        )
        if not self.selected_instruments or self.selected_instruments != tuple(
            sorted(set(self.selected_instruments))
        ):
            raise PublicIntegratedProfileError(
                "public_t04_receipt_selected_instruments_invalid"
            )
        if (
            not self.selected_quantities
            or self.selected_quantities != tuple(sorted(self.selected_quantities))
            or {key for key, _ in self.selected_quantities}
            != set(self.selected_instruments)
            or any(value <= 0 for _, value in self.selected_quantities)
        ):
            raise PublicIntegratedProfileError(
                "public_t04_receipt_selected_quantities_invalid"
            )
        if (
            self.selected_directions != tuple(sorted(self.selected_directions))
            or {key for key, _ in self.selected_directions}
            != set(self.selected_instruments)
            or any(
                value not in {Direction.LONG.value, Direction.SHORT.value}
                for _, value in self.selected_directions
            )
        ):
            raise PublicIntegratedProfileError(
                "public_t04_receipt_selected_directions_invalid"
            )
        if self.final_position_quantities != tuple(
            sorted(self.final_position_quantities)
        ):
            raise PublicIntegratedProfileError(
                "public_t04_receipt_final_positions_invalid"
            )
        for _key, numeric_value in (
            *self.final_position_quantities,
            *self.report_pnl_rows,
            *self.exposure_totals,
        ):
            _decimal(numeric_value, "public_t04_receipt.numeric_value")
        if (
            not self.component_hashes
            or self.component_hashes != tuple(sorted(self.component_hashes))
            or len({key for key, _ in self.component_hashes})
            != len(self.component_hashes)
        ):
            raise PublicIntegratedProfileError(
                "public_t04_receipt_component_hashes_invalid"
            )
        for _name, component_hash in self.component_hashes:
            _require_hash(
                component_hash,
                "public_t04_receipt.component_hash",
            )
        for values, label in (
            (self.breaches, "breaches"),
            (self.quality_flags, "quality_flags"),
        ):
            if values != tuple(sorted(set(values))):
                raise PublicIntegratedProfileError(
                    f"public_t04_receipt_{label}_invalid"
                )
        if not self.actions:
            raise PublicIntegratedProfileError("public_t04_receipt_actions_required")
        object.__setattr__(
            self,
            "content_hash",
            _component_hash(
                self.identity_payload(),
                label="public_integrated_t04_receipt",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": PUBLIC_T04_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "inputs_hash": self.inputs_hash,
            "provenance_hash": self.provenance_hash,
            "selected_instruments": list(self.selected_instruments),
            "selected_directions": [
                {"instrument_id": key, "direction": value}
                for key, value in self.selected_directions
            ],
            "selected_quantities": [
                {"instrument_id": key, "quantity": value}
                for key, value in self.selected_quantities
            ],
            "final_position_quantities": [
                {"instrument_id": key, "quantity": _decimal_text(value)}
                for key, value in self.final_position_quantities
            ],
            "closing_ledger_hash": self.closing_ledger_hash,
            "report_pnl_rows": {
                key: _decimal_text(value) for key, value in self.report_pnl_rows
            },
            "exposure_totals": {
                key: _decimal_text(value) for key, value in self.exposure_totals
            },
            "component_hashes": dict(self.component_hashes),
            "breaches": list(self.breaches),
            "actions": list(self.actions),
            "quality_flags": list(self.quality_flags),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


_COMPONENT_ERRORS = (
    AccountingReconciliationError,
    ExposureEngineError,
    ExpressionValidationError,
    MultiLegLedgerError,
    PortfolioAccountingError,
    ScenarioError,
)


def run_public_integrated_profile(
    inputs: PublicT04Inputs,
) -> PublicIntegratedProfileReceipt:
    """Execute the complete bounded institutional T-04 conformance path."""

    if not isinstance(inputs, PublicT04Inputs):
        raise PublicIntegratedProfileError("public_t04_inputs_required")
    try:
        return _run_public_integrated_profile(inputs)
    except PublicIntegratedProfileError:
        raise
    except _COMPONENT_ERRORS as exc:
        raise PublicIntegratedProfileError(
            f"public_t04_component_failure:{exc}"
        ) from exc


def _run_public_integrated_profile(
    inputs: PublicT04Inputs,
) -> PublicIntegratedProfileReceipt:
    candidate_decision, optimizer_decision, optimization_hash, quantities = (
        _select_and_optimize(inputs)
    )
    opening_ledger, funded_ledger = _build_funded_ledger(inputs)
    dynamic_plan, dynamic_result = _compile_and_execute(
        inputs,
        optimizer_decision,
        funded_ledger,
    )
    lifecycle_chain, lifecycle_ledger = _execute_lifecycle(
        inputs,
        dynamic_result.content_hash,
        dynamic_result.final_ledger,
    )
    (
        final_ledger,
        collateral_hash,
        funding_fx,
        accounting_reconciliation,
        report_reconciliation,
        advanced_accounting,
    ) = _execute_accounting(
        inputs,
        opening_ledger=opening_ledger,
        lifecycle_ledger=lifecycle_ledger,
    )
    exposure, extended_exposure = _execute_exposure(inputs, final_ledger)
    generated_path_hash, constrained_hash, path_result = _execute_stress(
        inputs,
        final_ledger,
    )

    breaches = set()
    if path_result.first_drawdown_breach_step_id is not None:
        breaches.add("DRAWDOWN:" + path_result.first_drawdown_breach_step_id)
    if path_result.first_margin_breach_step_id is not None:
        breaches.add("MARGIN:" + path_result.first_margin_breach_step_id)
    if path_result.first_liquidity_breach_step_id is not None:
        breaches.add("LIQUIDITY:" + path_result.first_liquidity_breach_step_id)
    collateral_result_hash = collateral_hash
    if advanced_accounting.collateral_waterfall_hash is None:
        raise PublicIntegratedProfileError(
            "public_t04_collateral_accounting_binding_missing"
        )
    actions = (
        "CANDIDATE_SELECTED:" + cast(str, candidate_decision.selected_candidate_id),
        "JOINT_INTEGER_OPTIMIZATION",
        "SEQUENTIAL_PARTIAL_UNWIND",
        "DYNAMIC_RETRY_FILLED",
        *tuple(
            f"LIFECYCLE:{item.action.value}" for item in lifecycle_chain.transitions
        ),
        "COLLATERAL_WATERFALL",
        "FUNDING_FX_REVALUATION",
        "TAX_LOT_PROJECTION",
        "HIGHER_ORDER_EXPOSURE",
        "CONSTRAINED_GENERATED_PATH_STRESS",
        "INDEPENDENT_LEDGER_REPORT_RECONCILIATION",
    )
    component_hashes = tuple(
        sorted(
            {
                "advanced_accounting": advanced_accounting.content_hash,
                "candidate_decision": candidate_decision.content_hash,
                "collateral_waterfall": collateral_result_hash,
                "constrained_scenarios": constrained_hash,
                "dynamic_execution": dynamic_result.content_hash,
                "dynamic_plan": dynamic_plan.content_hash,
                "extended_exposure": extended_exposure.content_hash,
                "final_ledger": final_ledger.content_hash,
                "funding_fx": funding_fx.content_hash,
                "generated_path": generated_path_hash,
                "input": inputs.content_hash,
                "ledger_reconciliation": accounting_reconciliation.content_hash,
                "lifecycle_chain": lifecycle_chain.content_hash,
                "optimization": optimization_hash,
                "path_stress": path_result.content_hash,
                "portfolio_exposure": exposure.content_hash,
                "provenance": inputs.provenance.content_hash,
                "report_reconciliation": report_reconciliation.content_hash,
            }.items()
        )
    )
    final_snapshot = final_ledger.replay()
    report_rows = tuple(sorted(accounting_reconciliation.report_rows().items()))
    exposure_totals = (
        ("collateral", exposure.totals.collateral),
        ("delta", exposure.totals.delta),
        ("gamma", exposure.totals.gamma),
        ("gross_notional", exposure.totals.gross_notional),
        ("margin", exposure.totals.margin),
        ("market_value", exposure.totals.market_value),
        ("net_notional", exposure.totals.net_notional),
        ("theta", exposure.totals.theta),
        ("vega", exposure.totals.vega),
        ("vanna", extended_exposure.totals.vanna),
        ("volga", extended_exposure.totals.volga),
    )
    quality_flags = tuple(
        sorted(
            {
                *inputs.provenance.quality_flags,
                "ADVANCED_ACCOUNTING_RECONCILED",
                "CALLER_SELECTION_REJECTED",
                "CONSTRAINED_STRESS_EXECUTED",
                "DETERMINISTIC_BOUNDED_EXECUTION",
                "MULTI_CURRENCY_FUNDING_REVALUED",
                "PARTIAL_FILL_REMEDIATED",
            }
        )
    )
    return PublicIntegratedProfileReceipt(
        profile_id=inputs.profile_id,
        inputs_hash=inputs.content_hash,
        provenance_hash=inputs.provenance.content_hash,
        selected_instruments=tuple(sorted(key for key, _ in quantities)),
        selected_directions=tuple(
            sorted(
                (
                    item.choice.instrument_id,
                    item.direction.value,
                )
                for item in inputs.research.optimization_legs
            )
        ),
        selected_quantities=quantities,
        final_position_quantities=tuple(
            sorted(
                (item.instrument_id, item.quantity) for item in final_snapshot.positions
            )
        ),
        closing_ledger_hash=final_ledger.content_hash,
        report_pnl_rows=report_rows,
        exposure_totals=exposure_totals,
        component_hashes=component_hashes,
        breaches=tuple(sorted(breaches)),
        actions=actions,
        quality_flags=quality_flags,
        _factory_token=_RECEIPT_TOKEN,
    )


def _select_and_optimize(
    inputs: PublicT04Inputs,
) -> tuple[ExpressionDecision, ExpressionDecision, str, tuple[tuple[str, int], ...]]:
    research = inputs.research
    distribution = research.hypothesis.distribution
    if any(
        not (
            distribution.downside_tail_return
            <= item.expected_return
            <= distribution.upside_return
        )
        for item in research.candidates
    ):
        raise PublicIntegratedProfileError(
            "public_t04_candidate_outside_expected_distribution"
        )
    as_of = research.optimization_legs[0].choice.known_at
    if any(item.choice.known_at != as_of for item in research.optimization_legs):
        raise PublicIntegratedProfileError("public_t04_optimization_leg_as_of_mismatch")
    engine = InstrumentExpressionEngine(research.expression_policy)
    candidate_decision = engine.select(
        hypothesis=research.hypothesis,
        payoff=research.payoff,
        candidates=research.candidates,
        as_of=as_of,
    )
    if candidate_decision.selected_candidate_id is None:
        raise PublicIntegratedProfileError(
            "public_t04_candidate_selection_infeasible:"
            + ",".join(candidate_decision.failure_evidence)
        )
    candidate = next(
        item
        for item in research.candidates
        if item.candidate_id == candidate_decision.selected_candidate_id
    )
    selected_ids = {item.instrument_id for item in candidate.choices}
    optimization_ids = {
        item.choice.instrument_id for item in research.optimization_legs
    }
    if selected_ids != optimization_ids:
        raise PublicIntegratedProfileError(
            "public_t04_optimizer_candidate_coverage_mismatch"
        )
    problem = JointOptimizationProblem(
        problem_id=f"{inputs.profile_id}.joint",
        hypothesis_hash=candidate_decision.hypothesis_hash,
        payoff_hash=candidate_decision.payoff_hash,
        policy_hash=candidate_decision.policy_hash,
        as_of=as_of,
        legs=research.optimization_legs,
        constraints=research.optimization_constraints,
        maximum_combinations=research.maximum_combinations,
    )
    result = DeterministicJointOptimizer().optimize(problem)
    if not result.feasible:
        reasons = ",".join(key for key, _count in result.infeasibility_reasons)
        feedback = ",".join(result.hypothesis_feedback)
        raise PublicIntegratedProfileError(
            f"public_t04_optimizer_infeasible:{reasons}:{feedback}"
        )
    optimizer_decision = result.as_expression_decision(as_of=as_of)
    quantities = tuple(sorted(result.quantities))
    if {key for key, _ in quantities} != selected_ids:
        raise PublicIntegratedProfileError(
            "public_t04_optimizer_selected_leg_coverage_mismatch"
        )
    return (
        candidate_decision,
        optimizer_decision,
        result.content_hash,
        quantities,
    )


def _build_funded_ledger(
    inputs: PublicT04Inputs,
) -> tuple[UnifiedPortfolioLedger, UnifiedPortfolioLedger]:
    accounting = inputs.accounting
    opening = UnifiedPortfolioLedger.open(
        ledger_id=f"{inputs.profile_id}.ledger",
        base_currency=accounting.base_currency,
    )
    ledger = opening
    for item in sorted(
        accounting.funding,
        key=lambda value: (value.occurred_at, value.funding_id),
    ):
        conversions: tuple[ExternalFlowConversionEvidence, ...] = ()
        if item.currency != accounting.base_currency:
            if item.conversion_rate is None or item.conversion_source_hash is None:
                raise PublicIntegratedProfileError(
                    "public_t04_nonbase_funding_conversion_required"
                )
            conversions = (
                ExternalFlowConversionEvidence(
                    currency=item.currency,
                    base_currency=accounting.base_currency,
                    observed_at=item.occurred_at,
                    fx_rate=item.conversion_rate,
                    source_hash=item.conversion_source_hash,
                ),
            )
        ledger = ledger.publish(
            funding_event(
                event_id=item.funding_id,
                occurred_at=item.occurred_at,
                cash_deltas=(CashDelta(item.currency, item.amount),),
                conversion_evidence=conversions,
                source_hashes=(item.source_hash,),
            )
        )
    return opening, ledger


def _compile_and_execute(
    inputs: PublicT04Inputs,
    decision: ExpressionDecision,
    ledger: UnifiedPortfolioLedger,
) -> tuple[DynamicMultiLegExecutionPlan, DynamicMultiLegExecutionResult]:
    execution = inputs.execution
    contracts = {item.contract_id: item for item in execution.contracts}
    compiler = ExpressionExecutionCompiler(execution.compile_policy)
    compiled_attempts = []
    attempts = execution.attempts
    selected_legs = {item.instrument_id: item for item in decision.selected_legs}
    if set(contracts) != set(selected_legs):
        raise PublicIntegratedProfileError(
            "public_t04_contract_selected_leg_coverage_mismatch"
        )
    for index, attempt in enumerate(attempts):
        quotes = {item.contract_id: item for item in attempt.quotes}
        if set(quotes) != set(selected_legs):
            raise PublicIntegratedProfileError(
                "public_t04_attempt_quote_selected_leg_coverage_mismatch"
            )
        leg_ids = {
            item.instrument_id: f"{attempt.execution_id}.leg.{position}"
            for position, item in enumerate(decision.selected_legs, start=1)
        }
        fill_times = {
            leg_ids[instrument_id]: value for instrument_id, value in attempt.fill_times
        }
        participation = {
            leg_ids[instrument_id]: value
            for instrument_id, value in attempt.participation_rates
        }
        compiled = compiler.compile(
            execution_id=attempt.execution_id,
            decision=decision,
            contracts=contracts,
            quotes=quotes,
            fill_times=fill_times,
            participation_rates=participation,
            fee_per_contract=execution.fee_per_contract,
            slippage_ticks=execution.slippage_ticks,
            unwind_at=attempt.unwind_at,
            unwind_fee_per_contract=execution.unwind_fee_per_contract,
        )
        moves = []
        for move in attempt.interleg_moves:
            if move.instrument_id not in selected_legs:
                raise PublicIntegratedProfileError(
                    "public_t04_interleg_move_instrument_unknown"
                )
            before = _executable_quote_price(
                quotes[move.instrument_id],
                selected_legs[move.instrument_id].direction,
            )
            if before != move.before_price:
                raise PublicIntegratedProfileError(
                    "public_t04_interleg_move_before_quote_inconsistent"
                )
            if move.before_state_hash != attempt.quote_state_hash or index + 1 >= len(
                attempts
            ):
                raise PublicIntegratedProfileError(
                    "public_t04_interleg_move_state_or_retry_missing"
                )
            next_attempt = attempts[index + 1]
            next_quotes = {item.contract_id: item for item in next_attempt.quotes}
            after = _executable_quote_price(
                next_quotes[move.instrument_id],
                selected_legs[move.instrument_id].direction,
            )
            if (
                move.after_price != after
                or move.after_state_hash != next_attempt.quote_state_hash
            ):
                raise PublicIntegratedProfileError(
                    "public_t04_interleg_move_after_quote_inconsistent"
                )
            moves.append(
                InterLegMarketMove(
                    leg_id=leg_ids[move.instrument_id],
                    before_price=move.before_price,
                    after_price=move.after_price,
                    before_state_hash=move.before_state_hash,
                    after_state_hash=move.after_state_hash,
                    exposure_before_hash=move.exposure_before_hash,
                    exposure_after_hash=move.exposure_after_hash,
                    cost_before_hash=move.cost_before_hash,
                    cost_after_hash=move.cost_after_hash,
                    margin_before_hash=move.margin_before_hash,
                    margin_after_hash=move.margin_after_hash,
                )
            )
        compiled_attempts.append(
            DynamicExecutionAttempt(
                attempt_number=index + 1,
                compiled=compiled,
                market_state_hash=attempt.market_state_hash,
                quote_state_hash=attempt.quote_state_hash,
                interleg_moves=tuple(sorted(moves, key=lambda item: item.leg_id)),
            )
        )
    plan = DynamicMultiLegExecutionPlan(
        plan_id=f"{inputs.profile_id}.dynamic",
        decision_hash=decision.content_hash,
        policy=execution.dynamic_policy,
        attempts=tuple(compiled_attempts),
    )
    result = DynamicMultiLegExecutionService().execute(
        plan,
        ledger=ledger,
        fx_rates=dict(execution.ledger_fx_rates),
    )
    if (
        len(result.attempts) < 2
        or result.attempts[0].disposition is not MultiLegDisposition.UNWOUND
        or result.final_execution.disposition is not MultiLegDisposition.FILLED
    ):
        raise PublicIntegratedProfileError(
            "public_t04_sequential_partial_retry_not_observed"
        )
    return plan, result


def _executable_quote_price(
    quote: OptionQuote,
    direction: Direction,
) -> Decimal:
    price = quote.ask if direction is Direction.LONG else quote.bid
    if price is None:
        raise PublicIntegratedProfileError("public_t04_executable_quote_price_missing")
    return price


def _position_hash(ledger: UnifiedPortfolioLedger) -> str:
    snapshot = ledger.replay()
    return _component_hash(
        [
            {
                "asset_class": item.asset_class.value,
                "instrument_id": item.instrument_id,
                "quantity": _decimal_text(item.quantity),
                "average_price": _decimal_text(item.average_price),
                "mark_price": _decimal_text(item.mark_price),
            }
            for item in snapshot.positions
        ],
        label="public_t04_ledger_position_exposure",
    )


def _margin_hash(ledger: UnifiedPortfolioLedger) -> str:
    return _component_hash(
        [
            {
                "instrument_id": item.instrument_id,
                "currency": item.currency,
                "amount": _decimal_text(item.amount),
            }
            for item in ledger.replay().margins
        ],
        label="public_t04_lifecycle_margin",
    )


def _execute_lifecycle(
    inputs: PublicT04Inputs,
    execution_hash: str,
    ledger: UnifiedPortfolioLedger,
) -> tuple[MultiLegLifecycleEvidenceChain, UnifiedPortfolioLedger]:
    contracts = {item.contract_id: item for item in inputs.execution.contracts}
    transitions = []
    predecessor = execution_hash
    current = ledger
    for sequence, rule in enumerate(inputs.accounting.lifecycle_rules, start=1):
        before = current
        before_snapshot = before.replay()
        positions = {item.instrument_id: item for item in before_snapshot.positions}
        source = positions.get(rule.source_instrument_id)
        if source is None or source.asset_class is not AssetClass.OPTION:
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_source_position_missing"
            )
        contract = contracts.get(rule.source_instrument_id)
        if (
            contract is None
            or contract.currency != source.currency
            or contract.multiplier != source.multiplier
        ):
            raise PublicIntegratedProfileError(
                "public_t04_lifecycle_source_contract_mismatch"
            )
        quantity_before = source.quantity
        drafts = []
        affected: tuple[str, ...]
        source_hashes = tuple(sorted((rule.content_hash, rule.source_hash)))
        if rule.action in {
            LifecycleActionType.HEDGE,
            LifecycleActionType.PARTIAL_UNWIND,
            LifecycleActionType.FULL_UNWIND,
        }:
            quantity_delta = -source.quantity * rule.fraction
            realized = _closing_realized_pnl(
                source.quantity,
                source.average_price,
                quantity_delta,
                rule.execution_price,
                source.multiplier,
            )
            drafts.append(
                trade_event(
                    event_id=f"{inputs.profile_id}.lifecycle.{sequence}.trade",
                    occurred_at=rule.occurred_at,
                    asset_class=AssetClass.OPTION,
                    instrument_id=source.instrument_id,
                    currency=source.currency,
                    quantity_delta=quantity_delta,
                    price=rule.execution_price,
                    multiplier=source.multiplier,
                    realized_pnl=realized,
                    source_hashes=source_hashes,
                )
            )
            quantity_after = source.quantity + quantity_delta
            affected = (source.instrument_id,)
        elif rule.action is LifecycleActionType.REBALANCE:
            quantity_delta = source.quantity * rule.fraction
            drafts.append(
                trade_event(
                    event_id=f"{inputs.profile_id}.lifecycle.{sequence}.trade",
                    occurred_at=rule.occurred_at,
                    asset_class=AssetClass.OPTION,
                    instrument_id=source.instrument_id,
                    currency=source.currency,
                    quantity_delta=quantity_delta,
                    price=rule.execution_price,
                    multiplier=source.multiplier,
                    source_hashes=source_hashes,
                )
            )
            quantity_after = source.quantity + quantity_delta
            affected = (source.instrument_id,)
        else:
            target_id = cast(str, rule.target_instrument_id)
            target_contract = contracts.get(target_id)
            if (
                target_contract is None
                or target_contract.currency != source.currency
                or target_contract.multiplier != source.multiplier
                or rule.target_price is None
            ):
                raise PublicIntegratedProfileError(
                    "public_t04_lifecycle_target_contract_mismatch"
                )
            closing_delta = -source.quantity
            drafts.extend(
                (
                    trade_event(
                        event_id=(f"{inputs.profile_id}.lifecycle.{sequence}.close"),
                        occurred_at=rule.occurred_at,
                        asset_class=AssetClass.OPTION,
                        instrument_id=source.instrument_id,
                        currency=source.currency,
                        quantity_delta=closing_delta,
                        price=rule.execution_price,
                        multiplier=source.multiplier,
                        realized_pnl=_closing_realized_pnl(
                            source.quantity,
                            source.average_price,
                            closing_delta,
                            rule.execution_price,
                            source.multiplier,
                        ),
                        source_hashes=source_hashes,
                    ),
                    trade_event(
                        event_id=(f"{inputs.profile_id}.lifecycle.{sequence}.open"),
                        occurred_at=rule.occurred_at,
                        asset_class=AssetClass.OPTION,
                        instrument_id=target_id,
                        currency=source.currency,
                        quantity_delta=source.quantity,
                        price=rule.target_price,
                        multiplier=source.multiplier,
                        source_hashes=source_hashes,
                    ),
                )
            )
            target_before = positions.get(target_id)
            quantity_after = source.quantity + (
                _ZERO if target_before is None else target_before.quantity
            )
            affected = tuple(sorted((source.instrument_id, target_id)))
        current = current.publish_many(tuple(drafts))
        transition = LifecycleTransitionEvidence(
            sequence=sequence,
            action=rule.action,
            trigger_id=rule.trigger_id,
            occurred_at=rule.occurred_at,
            predecessor_hash=predecessor,
            ledger_before_hash=before.content_hash,
            ledger_after_hash=current.content_hash,
            exposure_before_hash=_position_hash(before),
            exposure_after_hash=_position_hash(current),
            cost_hash=_component_hash(
                [item.identity_payload() for item in drafts],
                label="public_t04_lifecycle_cost",
            ),
            margin_hash=_margin_hash(current),
            affected_leg_ids=affected,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
        )
        transitions.append(transition)
        predecessor = transition.content_hash
    chain = MultiLegLifecycleEvidenceChain(
        execution_hash=execution_hash,
        transitions=tuple(transitions),
    )
    return chain, current


def _closing_realized_pnl(
    position_quantity: Decimal,
    average_price: Decimal,
    quantity_delta: Decimal,
    execution_price: Decimal,
    multiplier: Decimal,
) -> Decimal:
    if position_quantity * quantity_delta >= _ZERO:
        return _ZERO
    closed = min(abs(position_quantity), abs(quantity_delta))
    direction = _ONE if position_quantity > _ZERO else -_ONE
    return closed * (execution_price - average_price) * multiplier * direction


def _execute_accounting(
    inputs: PublicT04Inputs,
    *,
    opening_ledger: UnifiedPortfolioLedger,
    lifecycle_ledger: UnifiedPortfolioLedger,
) -> tuple[
    UnifiedPortfolioLedger,
    str,
    FundingFxRevaluation,
    LedgerPnlReconciliation,
    ReportLedgerReconciliation,
    AdvancedAccountingReconciliation,
]:
    accounting = inputs.accounting
    waterfall = allocate_collateral_waterfall(
        required_credit_base=accounting.required_collateral_base,
        assets=accounting.collateral_assets,
        fx_rates=dict(accounting.current_fx_rates) | {accounting.base_currency: _ONE},
        policy=accounting.collateral_policy,
    )
    factory = InvariantAccountingFactory(
        factory_id=f"{inputs.profile_id}.accounting",
        version="1",
    )
    collateral_bundle = factory.audit_bundle(
        event_id=f"{inputs.profile_id}.collateral.audit",
        event_type=PortfolioEventType.COLLATERAL_WATERFALL,
        occurred_at=accounting.collateral_at,
        economic_event_hashes=(waterfall.content_hash,),
        details={
            "provided_credit_base": _decimal_text(waterfall.provided_credit_base),
            "shortfall_base": _decimal_text(waterfall.default_shortfall_base),
        },
    )
    ledger = publish_advanced_accounting_bundle(
        lifecycle_ledger,
        collateral_bundle,
    )
    funding_fx = revalue_funding_fx(
        ledger,
        current_fx_rates=dict(accounting.current_fx_rates),
        current_fx_source_hash=accounting.current_fx_source_hash,
    )
    funding_bundle = factory.audit_bundle(
        event_id=f"{inputs.profile_id}.funding.fx.audit",
        event_type=PortfolioEventType.FUNDING_FX_REVALUATION,
        occurred_at=accounting.funding_fx_at,
        economic_event_hashes=(funding_fx.content_hash,),
        details={"translation_pnl": _decimal_text(funding_fx.total_translation_pnl)},
    )
    final_ledger = publish_advanced_accounting_bundle(ledger, funding_bundle)
    tax_lots = project_tax_lots(
        final_ledger,
        method=accounting.tax_lot_method,
    )
    ledger_reconciliation = LedgerPnlReconciliation.from_ledger_projection(
        reconciliation_id=f"{inputs.profile_id}.ledger.reconciliation",
        opening_ledger=opening_ledger,
        closing_ledger=final_ledger,
        opened_at=inputs.opened_at,
        closed_at=inputs.closed_at,
        fx_observations=accounting.fx_observations,
    )
    report_payload = encode_report_payload(
        report_id=f"{inputs.profile_id}.report",
        ledger=ledger_reconciliation,
    )
    report = ReportPnlSummary.from_json(
        report_payload,
        expected_payload_hash=report_payload_hash(report_payload),
    )
    report_reconciliation = ReportLedgerReconciliation(
        reconciliation_id=f"{inputs.profile_id}.report.reconciliation",
        ledger=ledger_reconciliation,
        report=report,
    )
    advanced = AdvancedAccountingReconciliation.from_ledger_evidence(
        ledger=final_ledger,
        ledger_reconciliation=ledger_reconciliation,
        tax_lots=tax_lots,
        funding_fx=funding_fx,
        collateral_waterfall=waterfall,
    )
    return (
        final_ledger,
        waterfall.content_hash,
        funding_fx,
        ledger_reconciliation,
        report_reconciliation,
        advanced,
    )


def _execute_exposure(
    inputs: PublicT04Inputs,
    ledger: UnifiedPortfolioLedger,
) -> tuple[PortfolioExposureSnapshot, ExtendedPortfolioExposure]:
    request = inputs.risk.exposure_request
    snapshot = MultiLegLedgerExecutionService.evaluate_ledger_exposure(
        ledger,
        request,
    )
    policy = inputs.risk.higher_order_policy
    supplements = tuple(
        PositionRiskSupplement(
            position_id=item.position_id,
            position_hash=item.position_hash,
            valuation_hash=item.valuation_hash,
            beta_equivalent=item.delta_base * policy.beta_multiplier,
            duration=item.gross_notional_base * policy.duration_fraction,
            dv01=(
                item.gross_notional_base * policy.duration_fraction * Decimal("0.0001")
            ),
            fx_exposure=item.net_notional_base,
            commodity_units=item.quantity * item.multiplier,
            vanna=item.gamma_base * policy.vanna_multiplier,
            volga=item.vega_base * policy.volga_multiplier,
            charm=item.theta_base * policy.charm_multiplier,
            factor_ids=(f"factor:{item.underlying_id}",),
            funding_bucket=f"funding:{item.native_currency.lower()}",
            tenor_bucket=(
                "tenor:expiring" if item.expiry_at is not None else "tenor:non_expiring"
            ),
            volatility_bucket=(
                "vol:option"
                if item.instrument_kind is InstrumentKind.OPTION
                else "vol:non_option"
            ),
            source_hashes=tuple(sorted({policy.content_hash, policy.source_hash})),
        )
        for item in snapshot.positions
    )
    extended = build_extended_portfolio_exposure(
        snapshot,
        supplements=supplements,
        offset_policy=inputs.risk.offset_policy,
    )
    return snapshot, extended


def _execute_stress(
    inputs: PublicT04Inputs,
    ledger: UnifiedPortfolioLedger,
) -> tuple[str, str, PathScenarioResult]:
    stress = inputs.stress
    generated = ScenarioPathFactory().generate(
        stress.generation_spec,
        observations=stress.observations,
    )
    market_state = inputs.risk.exposure_request.market_state
    snapshot = ledger.replay()
    constrained = ConstrainedJointScenarioEngine(policy=stress.economic_policy)
    constrained_hashes = []
    for event, effective_at in zip(
        generated.events,
        stress.effective_times,
        strict=True,
    ):
        result = constrained.evaluate(
            snapshot,
            market_state=market_state,
            shock=event.shock,
            base_liquidation_costs=dict(stress.base_liquidation_costs),
            scenario_valuation_at=effective_at,
        )
        constrained_hashes.append(result.content_hash)
    constrained_hash = _component_hash(
        constrained_hashes,
        label="public_t04_constrained_generated_scenarios",
    )
    scenario = generated.to_stress_scenario(
        expected_base_state_hash=market_state.state_hash(),
        expected_ledger_hash=snapshot.ledger_hash,
        effective_times=stress.effective_times,
        risk_limits=stress.risk_limits,
    )
    path_result = PathScenarioEngine(max_steps=MAX_T04_STRESS_STEPS).evaluate(
        snapshot,
        market_state=market_state,
        scenario=scenario,
        base_liquidation_costs=dict(stress.base_liquidation_costs),
    )
    return generated.content_hash, constrained_hash, path_result


def _fixture_hash(
    *,
    source_document_id: str,
    source_document_hashes: tuple[str, ...],
    label: str,
) -> str:
    return _component_hash(
        {
            "source_document_id": source_document_id,
            "source_document_hashes": list(source_document_hashes),
            "fixture_component": label,
        },
        label="public_t04_fixture_source",
    )


def _fixture_timestamp(opened: datetime, seconds: int) -> str:
    return _timestamp_text(opened + timedelta(seconds=seconds))


def _fixture_availability(observed_at: str) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=observed_at,
        published_at=observed_at,
        provider_received_at=observed_at,
        system_received_at=observed_at,
        processed_at=observed_at,
    )


def build_public_t04_fixture_inputs(
    *,
    source_document_id: str,
    source_document_hashes: Sequence[str],
    opened_at: str,
    closed_at: str,
    underlying_id: str = "T04.UNDERLYING",
    leg_instrument_ids: tuple[str, str] = (
        "T04.OPTION.A",
        "T04.OPTION.B",
    ),
    leg_prices: tuple[Decimal, Decimal] = (
        Decimal("6"),
        Decimal("4"),
    ),
    leg_directions: tuple[Direction, Direction] = (
        Direction.LONG,
        Direction.LONG,
    ),
    target_notional: Decimal = Decimal("20000"),
    base_currency: str = "USD",
    funding_currency: str = "EUR",
) -> PublicT04Inputs:
    """Create a bounded, externally prepared immutable synthetic T-04 fixture.

    This factory exists for conformance tests and examples.  It creates only
    primitive inputs; the public runner still selects instruments, optimizes
    quantities, executes orders, and creates every receipt itself.
    """

    document_id = _require_id(
        source_document_id,
        "public_t04_fixture.source_document_id",
    )
    source_hashes = tuple(source_document_hashes)
    if not source_hashes or len(set(source_hashes)) != len(source_hashes):
        raise PublicIntegratedProfileError("public_t04_fixture_source_hashes_invalid")
    for document_hash in source_hashes:
        _require_hash(
            document_hash,
            "public_t04_fixture.source_document_hash",
        )
    source_hashes = tuple(sorted(source_hashes))
    opened = _timestamp(opened_at, "public_t04_fixture.opened_at")
    closed = _timestamp(closed_at, "public_t04_fixture.closed_at")
    if (closed - opened).total_seconds() < 180:
        raise PublicIntegratedProfileError("public_t04_fixture_window_too_short")
    if (
        not _CURRENCY.fullmatch(base_currency)
        or not _CURRENCY.fullmatch(funding_currency)
        or base_currency == funding_currency
    ):
        raise PublicIntegratedProfileError("public_t04_fixture_currency_pair_invalid")
    leg_ids = tuple(leg_instrument_ids)
    if len(leg_ids) != 2 or leg_ids != tuple(sorted(set(leg_ids))):
        raise PublicIntegratedProfileError("public_t04_fixture_leg_ids_invalid")
    for leg_id in leg_ids:
        _require_id(leg_id, "public_t04_fixture.leg_id")
    prices = tuple(leg_prices)
    if len(prices) != 2:
        raise PublicIntegratedProfileError("public_t04_fixture_leg_prices_invalid")
    for index, leg_price in enumerate(prices):
        _decimal(
            leg_price,
            f"public_t04_fixture.leg_price.{index}",
            positive=True,
        )
        if leg_price <= Decimal("0.02"):
            raise PublicIntegratedProfileError(
                "public_t04_fixture_leg_price_below_tick"
            )
    directions = tuple(leg_directions)
    if len(directions) != 2 or any(
        not isinstance(item, Direction) for item in directions
    ):
        raise PublicIntegratedProfileError("public_t04_fixture_leg_directions_invalid")
    _decimal(
        target_notional,
        "public_t04_fixture.target_notional",
        positive=True,
    )
    _require_id(underlying_id, "public_t04_fixture.underlying_id")
    spot_id = _require_id(
        f"{underlying_id}.SPOT",
        "public_t04_fixture.spot_id",
    )
    opened_text = _timestamp_text(opened)
    closed_text = _timestamp_text(closed)
    expiry = closed + timedelta(days=180)
    expiry_text = _timestamp_text(expiry)
    listing_text = _timestamp_text(opened - timedelta(days=1))
    settlement_text = _timestamp_text(expiry + timedelta(hours=1))
    market_at = _fixture_timestamp(opened, 110)
    first_stress_at = _fixture_timestamp(opened, 120)
    second_stress_at = _fixture_timestamp(opened, 130)
    profile_seed = _fixture_hash(
        source_document_id=document_id,
        source_document_hashes=source_hashes,
        label="profile",
    )
    profile_id = f"public.t04.{profile_seed.removeprefix('sha256:')[:16]}"

    def source_hash(label: str) -> str:
        return _fixture_hash(
            source_document_id=document_id,
            source_document_hashes=source_hashes,
            label=label,
        )

    provenance = PublicT04Provenance(
        source_document_id=document_id,
        kind=(
            PublicProfileProvenanceKind.EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE
        ),
        source_document_hashes=source_hashes,
        quality_flags=(
            "EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE",
            "OFFLINE_SOURCE_BOUND",
        ),
    )

    distribution = ExpectedMarketDistribution(
        expected_return=Decimal("0.06"),
        annualized_volatility=Decimal("0.25"),
        downside_tail_return=Decimal("-0.20"),
        upside_return=Decimal("0.25"),
        horizon_days=60,
        risk_free_rate=Decimal("0.03"),
        dividend_yield=Decimal("0.01"),
        volatility_change=Decimal("0.02"),
        liquidity_change=Decimal("-0.05"),
        scenarios=(
            ScenarioRange(
                name="bear",
                probability=Decimal("0.25"),
                lower_return=Decimal("-0.20"),
                upper_return=Decimal("-0.05"),
            ),
            ScenarioRange(
                name="base",
                probability=Decimal("0.50"),
                lower_return=Decimal("-0.05"),
                upper_return=Decimal("0.12"),
            ),
            ScenarioRange(
                name="bull",
                probability=Decimal("0.25"),
                lower_return=Decimal("0.12"),
                upper_return=Decimal("0.25"),
            ),
        ),
    )
    hypothesis = EconomicHypothesis(
        hypothesis_id=f"{profile_id}.hypothesis",
        version="1",
        economic_underlying_id=underlying_id,
        rationale=(
            "Externally prepared immutable evidence supports bounded upside "
            "with explicit tail protection."
        ),
        expected_direction=Direction.LONG,
        distribution=distribution,
        conditions=("immutable evidence remains inside the reviewed regime",),
        failure_conditions=("reviewed downside boundary is breached",),
        prediction_target="60-day total return distribution",
        evaluation_metrics=("net return", "drawdown", "tail loss"),
        data_limitations=("synthetic fixture used only for conformance",),
        model_risks=("nonlinear option approximation",),
    )
    direction_sum = sum(
        (item.sign for item in directions),
        Decimal("0"),
    )
    target_delta = Decimal("10") * direction_sum
    target_gamma = Decimal("2") * direction_sum
    target_vega = Decimal("3") * direction_sum
    target_theta = Decimal("-1") * direction_sum
    payoff = DesiredEconomicPayoff(
        underlying_id=underlying_id,
        direction=Direction.LONG,
        horizon_days=60,
        target_notional=target_notional,
        target_delta=target_delta,
        target_vega=target_vega,
        target_volatility=Decimal("0.25"),
        maximum_loss=target_notional,
        maximum_premium=target_notional * Decimal("0.25"),
        tail_protection_required=True,
        bounded_loss_required=True,
        allowed_expression_kinds=(ExpressionKind.OPTION_SPREAD,),
    )
    spot_price = target_notional / Decimal("200")
    unit_notional = target_notional / Decimal("2")
    choices = tuple(
        InstrumentChoice(
            instrument_id=instrument_id,
            economic_underlying_id=underlying_id,
            product_kind=ProductKind.OPTION,
            currency=base_currency,
            known_at=opened,
            unit_price=price,
            contract_multiplier=Decimal("100"),
            economic_notional_per_unit=unit_notional,
            liquidity_score=Decimal("0.90"),
            expected_return=Decimal("0.06"),
            expected_carry=Decimal("0.01"),
            expected_roll_cost=Decimal("0.002"),
            expected_time_value_decay=Decimal("0.01"),
            implied_volatility=Decimal("0.25"),
            transaction_cost=target_notional * Decimal("0.0005"),
            initial_margin=target_notional * Decimal("0.025"),
            tail_loss=Decimal("0.20"),
            model_sensitivity=Decimal("0.10"),
            data_confidence=Decimal("0.95"),
            expiry=expiry,
            strike=spot_price,
            delta=Decimal("0.50"),
            vega=Decimal("0.20"),
            option_right="CALL",
        )
        for instrument_id, price in zip(leg_ids, prices, strict=True)
    )
    selection_rules = tuple(
        LegSelectionRule(
            product_kind=ProductKind.OPTION,
            minimum_days_to_expiry=1,
            target_delta=Decimal("0.50"),
            target_vega=Decimal("0.20"),
            target_moneyness=Decimal("1"),
            minimum_liquidity_score=Decimal("0.70"),
            sizing_method="TARGET_NOTIONAL",
        )
        for _instrument_id in leg_ids
    )
    candidate = ExpressionCandidate(
        candidate_id=f"{profile_id}.candidate.option.spread",
        expression_kind=ExpressionKind.OPTION_SPREAD,
        choices=choices,
        directions=directions,
        roles=(LegRole.PRIMARY, LegRole.TAIL_PROTECTION),
        leg_ratios=(Decimal("0.5"), Decimal("0.5")),
        selection_rules=selection_rules,
        execution_mode=ExecutionMode.SEQUENTIAL,
        expected_return=Decimal("0.06"),
        pnl_dispersion=Decimal("0.15"),
        maximum_loss=target_notional * Decimal("0.50"),
        carry=Decimal("0.01"),
        roll_cost=Decimal("0.002"),
        time_value_decay=Decimal("0.01"),
        implied_volatility_cost=Decimal("0.01"),
        liquidity_score=Decimal("0.90"),
        transaction_cost=target_notional * Decimal("0.001"),
        margin_required=target_notional * Decimal("0.05"),
        tail_risk=Decimal("0.10"),
        model_sensitivity=Decimal("0.10"),
        data_confidence=Decimal("0.95"),
        targets=StrategyTargets(
            net_delta=target_delta,
            net_vega=target_vega,
            net_gamma=target_gamma,
            target_notional=target_notional,
            maximum_premium=target_notional * Decimal("0.25"),
            maximum_loss=target_notional * Decimal("0.50"),
            collateral_limit=target_notional * Decimal("0.25"),
            cash_limit=target_notional,
        ),
        legging_risk_limit=target_notional * Decimal("0.01"),
        maximum_leg_time_skew_seconds=5,
        allow_partial_fill=True,
    )
    optimization_legs = tuple(
        OptimizationLeg(
            choice=choice,
            selection_rule=rule,
            direction=direction,
            role=role,
            minimum_quantity=1,
            maximum_quantity=3,
            quantity_step=1,
            target_ratio=Decimal("1"),
            unit_delta=Decimal("10"),
            unit_gamma=Decimal("2"),
            unit_vega=Decimal("3"),
            unit_theta=Decimal("-1"),
            unit_maximum_loss=target_notional * Decimal("0.10"),
            unit_capital=target_notional * Decimal("0.05"),
            unit_margin=target_notional * Decimal("0.025"),
            unit_turnover=target_notional * Decimal("0.005"),
            concentration_group=choice.instrument_id,
        )
        for choice, rule, direction, role in zip(
            choices,
            selection_rules,
            directions,
            (LegRole.PRIMARY, LegRole.TAIL_PROTECTION),
            strict=True,
        )
    )
    research = PublicT04ResearchInputs(
        hypothesis=hypothesis,
        payoff=payoff,
        candidates=(candidate,),
        optimization_legs=optimization_legs,
        optimization_constraints=JointOptimizationConstraints(
            target_delta=target_delta,
            target_gamma=target_gamma,
            target_vega=target_vega,
            target_theta=target_theta,
            target_notional=target_notional,
            maximum_loss=target_notional * Decimal("0.25"),
            capital_limit=target_notional * Decimal("0.12"),
            margin_limit=target_notional * Decimal("0.06"),
            turnover_limit=target_notional * Decimal("0.02"),
            maximum_concentration=Decimal("0.50"),
            minimum_liquidity_score=Decimal("0.70"),
            ratio_tolerance=Decimal("0"),
        ),
        maximum_combinations=64,
    )

    contracts = tuple(
        OptionContract(
            contract_id=instrument_id,
            underlying_id=spot_id,
            option_type=OptionType.CALL,
            strike=spot_price,
            expiration_at=expiry_text,
            exercise_style=ExerciseStyle.EUROPEAN,
            settlement_type=OptionSettlementType.CASH,
            multiplier=Decimal("100"),
            currency=base_currency,
            exchange="offline.fixture",
            listing_at=listing_text,
            last_trade_at=expiry_text,
            settlement_at=settlement_text,
            price_tick=Decimal("0.01"),
        )
        for instrument_id in leg_ids
    )

    def execution_quotes(
        execution_id: str,
        *,
        observed_at: str,
        price_shift: Decimal,
        second_ask_size: Decimal,
    ) -> tuple[OptionQuote, ...]:
        return tuple(
            OptionQuote(
                quote_id=f"{execution_id}.quote.{index}",
                contract_id=contract.contract_id,
                availability=_fixture_availability(observed_at),
                as_of=observed_at,
                bid=price - Decimal("0.01") + price_shift,
                ask=price + price_shift,
                last=price - Decimal("0.005") + price_shift,
                bid_size=(
                    second_ask_size
                    if (index == 2 and directions[index - 1] is Direction.SHORT)
                    else Decimal("10")
                ),
                ask_size=(
                    second_ask_size
                    if (index == 2 and directions[index - 1] is Direction.LONG)
                    else Decimal("10")
                ),
                volume=1000,
                open_interest=5000,
            )
            for index, (contract, price) in enumerate(
                zip(contracts, prices, strict=True),
                start=1,
            )
        )

    first_execution_id = f"{profile_id}.execution.1"
    second_execution_id = f"{profile_id}.execution.2"
    first_quote_state = source_hash("attempt.1.quote.state")
    second_quote_state = source_hash("attempt.2.quote.state")
    first_quotes = execution_quotes(
        first_execution_id,
        observed_at=_fixture_timestamp(opened, 30),
        price_shift=Decimal("0"),
        second_ask_size=Decimal("0.5"),
    )
    second_quotes = execution_quotes(
        second_execution_id,
        observed_at=_fixture_timestamp(opened, 40),
        price_shift=Decimal("0.01"),
        second_ask_size=Decimal("10"),
    )
    attempts = (
        PublicExecutionAttempt(
            execution_id=first_execution_id,
            quotes=first_quotes,
            fill_times=(
                (leg_ids[0], _fixture_timestamp(opened, 30)),
                (leg_ids[1], _fixture_timestamp(opened, 31)),
            ),
            participation_rates=(),
            market_state_hash=source_hash("attempt.1.market.state"),
            quote_state_hash=first_quote_state,
            unwind_at=_fixture_timestamp(opened, 32),
            interleg_moves=(
                PublicInterLegMoveInput(
                    instrument_id=leg_ids[1],
                    before_price=(
                        prices[1]
                        if directions[1] is Direction.LONG
                        else prices[1] - Decimal("0.01")
                    ),
                    after_price=(
                        prices[1] + Decimal("0.01")
                        if directions[1] is Direction.LONG
                        else prices[1]
                    ),
                    before_state_hash=first_quote_state,
                    after_state_hash=second_quote_state,
                    exposure_before_hash=source_hash("interleg.exposure.before"),
                    exposure_after_hash=source_hash("interleg.exposure.after"),
                    cost_before_hash=source_hash("interleg.cost.before"),
                    cost_after_hash=source_hash("interleg.cost.after"),
                    margin_before_hash=source_hash("interleg.margin.before"),
                    margin_after_hash=source_hash("interleg.margin.after"),
                ),
            ),
        ),
        PublicExecutionAttempt(
            execution_id=second_execution_id,
            quotes=second_quotes,
            fill_times=(
                (leg_ids[0], _fixture_timestamp(opened, 40)),
                (leg_ids[1], _fixture_timestamp(opened, 41)),
            ),
            participation_rates=(),
            market_state_hash=source_hash("attempt.2.market.state"),
            quote_state_hash=second_quote_state,
            unwind_at=_fixture_timestamp(opened, 42),
        ),
    )
    execution = PublicT04ExecutionInputs(
        contracts=contracts,
        compile_policy=ExpressionCompilePolicy(
            compiler_id=f"{profile_id}.compiler",
            version="1",
            execution_policy=MultiLegExecutionPolicy.SEQUENTIAL,
            maximum_leg_time_skew_seconds=5,
            allow_partial=True,
            sequential_partial_action=SequentialPartialAction.UNWIND,
        ),
        dynamic_policy=DynamicExecutionPolicy(
            policy_id=f"{profile_id}.dynamic.policy",
            version="1",
            time_in_force=DynamicTimeInForce.TIMEOUT,
            maximum_attempts=2,
            timeout_seconds=20,
            retry_delay_seconds=5,
            accept_partial=False,
            maximum_interleg_move_bps=Decimal("10000"),
        ),
        attempts=attempts,
        ledger_fx_rates=tuple(
            sorted(
                (
                    (base_currency, Decimal("1")),
                    (funding_currency, Decimal("1.10")),
                )
            )
        ),
        fee_per_contract=Decimal("0.50"),
        unwind_fee_per_contract=Decimal("0.25"),
    )

    funding_fx_source = source_hash("funding.fx.lock")
    current_fx_source = source_hash("funding.fx.current")
    lifecycle_rules = (
        PublicLifecycleRule(
            action=LifecycleActionType.HEDGE,
            trigger_id=f"{profile_id}.trigger.hedge",
            occurred_at=_fixture_timestamp(opened, 50),
            source_instrument_id=leg_ids[1],
            execution_price=prices[1] + Decimal("0.02"),
            fraction=Decimal("0.25"),
            source_hash=source_hash("lifecycle.hedge"),
        ),
        PublicLifecycleRule(
            action=LifecycleActionType.REBALANCE,
            trigger_id=f"{profile_id}.trigger.rebalance",
            occurred_at=_fixture_timestamp(opened, 60),
            source_instrument_id=leg_ids[0],
            execution_price=prices[0] + Decimal("0.02"),
            fraction=Decimal("0.25"),
            source_hash=source_hash("lifecycle.rebalance"),
        ),
        PublicLifecycleRule(
            action=LifecycleActionType.ROLL_EXPIRY,
            trigger_id=f"{profile_id}.trigger.roll",
            occurred_at=_fixture_timestamp(opened, 70),
            source_instrument_id=leg_ids[0],
            execution_price=prices[0] + Decimal("0.03"),
            fraction=Decimal("1"),
            source_hash=source_hash("lifecycle.roll"),
            target_instrument_id=leg_ids[1],
            target_price=prices[1] + Decimal("0.03"),
        ),
        PublicLifecycleRule(
            action=LifecycleActionType.PARTIAL_UNWIND,
            trigger_id=f"{profile_id}.trigger.partial.unwind",
            occurred_at=_fixture_timestamp(opened, 80),
            source_instrument_id=leg_ids[1],
            execution_price=prices[1] + Decimal("0.04"),
            fraction=Decimal("0.25"),
            source_hash=source_hash("lifecycle.partial.unwind"),
        ),
    )
    accounting = PublicT04AccountingInputs(
        base_currency=base_currency,
        funding=(
            PublicFundingInput(
                funding_id=f"{profile_id}.funding.base",
                occurred_at=_fixture_timestamp(opened, 10),
                currency=base_currency,
                amount=target_notional * Decimal("5"),
                source_hash=source_hash("funding.base"),
            ),
            PublicFundingInput(
                funding_id=f"{profile_id}.funding.nonbase",
                occurred_at=_fixture_timestamp(opened, 20),
                currency=funding_currency,
                amount=Decimal("1000"),
                source_hash=source_hash("funding.nonbase"),
                conversion_rate=Decimal("1.10"),
                conversion_source_hash=funding_fx_source,
            ),
        ),
        lifecycle_rules=lifecycle_rules,
        collateral_assets=(
            CollateralAssetBalance(
                asset_id=f"{profile_id}.collateral.base",
                currency=base_currency,
                market_value=Decimal("800"),
                haircut=Decimal("0"),
                eligible=True,
                priority=1,
                source_hash=source_hash("collateral.base"),
            ),
            CollateralAssetBalance(
                asset_id=f"{profile_id}.collateral.nonbase",
                currency=funding_currency,
                market_value=Decimal("400"),
                haircut=Decimal("0.10"),
                eligible=True,
                priority=2,
                source_hash=source_hash("collateral.nonbase"),
            ),
        ),
        collateral_policy=CollateralWaterfallPolicy(
            policy_id=f"{profile_id}.collateral.policy",
            version="1",
            maximum_single_asset_fraction=Decimal("0.80"),
            allow_default_shortfall=False,
        ),
        required_collateral_base=Decimal("1000"),
        collateral_at=_fixture_timestamp(opened, 90),
        funding_fx_at=_fixture_timestamp(opened, 100),
        current_fx_rates=((funding_currency, Decimal("1.20")),),
        current_fx_source_hash=current_fx_source,
        fx_observations=(
            PitFxObservation(
                observation_id=f"{profile_id}.fx.open",
                currency=funding_currency,
                base_currency=base_currency,
                observed_at=opened_text,
                rate=Decimal("1.10"),
                source_hash=source_hash("fx.open"),
            ),
            PitFxObservation(
                observation_id=f"{profile_id}.fx.funding",
                currency=funding_currency,
                base_currency=base_currency,
                observed_at=_fixture_timestamp(opened, 20),
                rate=Decimal("1.10"),
                source_hash=funding_fx_source,
            ),
            PitFxObservation(
                observation_id=f"{profile_id}.fx.close",
                currency=funding_currency,
                base_currency=base_currency,
                observed_at=closed_text,
                rate=Decimal("1.20"),
                source_hash=current_fx_source,
            ),
        ),
        tax_lot_method=TaxLotMethod.FIFO,
    )

    product_source = SourceReference(
        source_id=f"{profile_id}.product.master",
        source_version="1",
        content_hash=source_hash("product.master"),
        observed_at=opened_text,
        source_uri=f"immutable-fixture://{document_id}",
    )
    validity = EffectivePeriod(
        valid_from=listing_text,
        valid_to=_timestamp_text(expiry + timedelta(days=1)),
    )
    instruments = (
        Instrument(
            instrument_id=spot_id,
            kind=InstrumentKind.SPOT,
            name="Public T-04 underlying reference",
            economic_underlying_id=underlying_id,
            currency=base_currency,
            unit="share",
            validity=validity,
            source=product_source,
        ),
        *tuple(
            Instrument(
                instrument_id=instrument_id,
                kind=InstrumentKind.OPTION,
                name=f"Public T-04 option leg {index}",
                economic_underlying_id=underlying_id,
                currency=base_currency,
                unit="contract",
                validity=validity,
                source=product_source,
            )
            for index, instrument_id in enumerate(leg_ids, start=1)
        ),
    )
    registry = InstrumentRegistry(
        economic_underlyings=(
            EconomicUnderlying(
                underlying_id=underlying_id,
                name="Public T-04 research underlying",
                asset_class="equity",
                unit="share",
                currency=base_currency,
                validity=validity,
                source=product_source,
            ),
        ),
        instruments=instruments,
        contract_specifications=tuple(
            ContractSpecification(
                contract_specification_id=f"{profile_id}.spec.{index}",
                instrument_id=instrument_id,
                contract_multiplier=Decimal("100"),
                contract_unit="share",
                settlement_type=DomainSettlementType.CASH,
                settlement_currency=base_currency,
                expiry_at=expiry_text,
                last_trade_at=expiry_text,
                exercise_style="EUROPEAN",
                minimum_tick=Decimal("0.01"),
                tick_value=Decimal("1"),
                trading_currency=base_currency,
                calendar_id="PUBLIC.T04",
                lifecycle_rule_id="option.european.cash.v1",
                validity=validity,
                source=product_source,
            )
            for index, instrument_id in enumerate(leg_ids, start=1)
        ),
        relationships=tuple(
            InstrumentRelationship(
                relationship_id=f"{profile_id}.relationship.{index}",
                source_instrument_id=instrument_id,
                target_instrument_id=spot_id,
                relationship_type=(InstrumentRelationshipType.OPTION_UNDERLYING),
                validity=validity,
                source=product_source,
            )
            for index, instrument_id in enumerate(leg_ids, start=1)
        ),
    )
    metadata = ObservationMetadata(
        observed_at=market_at,
        knowledge_at=market_at,
        source_hash=source_hash("market.state"),
        calendar_id="PUBLIC.T04",
        max_age_seconds=0,
    )
    analytics_price_cap = spot_price * Decimal("0.50")
    market_prices = (
        min(prices[0] + Decimal("0.03"), analytics_price_cap),
        min(prices[1] + Decimal("0.04"), analytics_price_cap),
    )
    valuation_availability = _fixture_availability(market_at)
    valuation_quotes = tuple(
        OptionQuote(
            quote_id=f"{profile_id}.valuation.quote.{index}",
            contract_id=instrument_id,
            availability=valuation_availability,
            as_of=market_at,
            bid=price - Decimal("0.01"),
            ask=price + Decimal("0.01"),
            last=price,
            bid_size=Decimal("50"),
            ask_size=Decimal("50"),
            volume=1000,
            open_interest=5000,
        )
        for index, (instrument_id, price) in enumerate(
            zip(leg_ids, market_prices, strict=True),
            start=1,
        )
    )
    option_market_quotes = tuple(
        OptionContractQuote(
            contract_id=quote.contract_id,
            underlying_instrument_id=spot_id,
            expiry_at=expiry_text,
            right=OptionRight.CALL,
            strike=spot_price,
            currency=base_currency,
            price_unit=f"{base_currency}_per_share",
            bid=price - Decimal("0.01"),
            ask=price + Decimal("0.01"),
            last=quote.last,
            settlement=None,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            volume=Decimal(quote.volume),
            open_interest=Decimal(quote.open_interest),
            condition=QuoteCondition.NORMAL,
            metadata=ObservationMetadata(
                observed_at=quote.availability.event_at,
                knowledge_at=quote.availability.processed_at,
                source_hash=quote.content_hash,
                calendar_id="PUBLIC.T04",
                max_age_seconds=quote.stale_after_seconds,
            ),
        )
        for quote, price in zip(valuation_quotes, market_prices, strict=True)
    )
    contracts_by_id = {item.contract_id: item for item in contracts}
    valuation_inputs = tuple(
        ValuationInputSnapshot(
            valuation_input_id=f"{profile_id}.valuation.input.{index}",
            contract=contracts_by_id[quote.contract_id],
            quote=quote,
            valuation_at=market_at,
            spot_price=spot_price,
            risk_free_rate=Decimal("0"),
            dividend_yield=Decimal("0"),
            forward_price=spot_price,
            spot_availability=valuation_availability,
            rate_availability=valuation_availability,
            dividend_availability=valuation_availability,
            forward_availability=valuation_availability,
            source_manifest_hashes=source_hashes,
        )
        for index, quote in enumerate(valuation_quotes, start=1)
    )
    margin_model_hash = source_hash("option.margin.model")
    analytics_factory = AuthoritativeOptionAnalyticsFactory(
        registry=default_option_model_registry(),
        comparison_policy=default_analytics_comparison_policy(),
        margin_model_hash=margin_model_hash,
    )
    pricing_model_hash = analytics_factory.pricing_adapter.model.content_hash
    model_specification_hash = (
        analytics_factory.pricing_adapter.specification.content_hash
    )
    option_analytics = tuple(
        analytics_factory.derive(
            receipt_id=f"{profile_id}.analytics.receipt.{index}",
            quote=quote,
            valuation_input=valuation_input,
            margin_per_contract=Decimal("200"),
            collateral_per_contract=Decimal("100"),
        ).analytics_mark
        for index, (quote, valuation_input) in enumerate(
            zip(option_market_quotes, valuation_inputs, strict=True),
            start=1,
        )
    )
    market_state = MarketState(
        state_id=f"{profile_id}.market.state",
        valuation_at=market_at,
        base_currency=base_currency,
        calendar_ids=("PUBLIC.T04",),
        spots=(
            SpotQuote(
                instrument_id=spot_id,
                price=spot_price,
                currency=base_currency,
                unit=f"{base_currency}_per_share",
                metadata=metadata,
            ),
        ),
        fx_quotes=(
            FXQuote(
                base_currency=funding_currency,
                quote_currency=base_currency,
                rate=Decimal("1.10"),
                unit=f"{base_currency}_per_{funding_currency}",
                metadata=metadata,
            ),
        ),
        liquidity_quotes=tuple(
            LiquidityQuote(
                instrument_id=instrument_id,
                currency=base_currency,
                bid=price - Decimal("0.01"),
                ask=price + Decimal("0.01"),
                price_unit=f"{base_currency}_per_share",
                depth_quantity=Decimal("50"),
                quantity_unit="contract",
                metadata=metadata,
            )
            for instrument_id, price in zip(
                leg_ids,
                market_prices,
                strict=True,
            )
        ),
        option_chains=(
            OptionChainState(
                chain_id=f"{profile_id}.option.chain",
                underlying_instrument_id=spot_id,
                currency=base_currency,
                price_unit=f"{base_currency}_per_share",
                quotes=option_market_quotes,
                analytics=option_analytics,
                metadata=metadata,
            ),
        ),
    )
    risk = PublicT04RiskInputs(
        exposure_request=LedgerExposureRequest(
            snapshot_id=f"{profile_id}.exposure",
            engine=ExposureEngine(
                product_catalog=registry,
                adapters=(
                    OptionValuationAdapter(
                        pricing_model_hash=pricing_model_hash,
                        model_specification_hash=model_specification_hash,
                        margin_model_hash=margin_model_hash,
                    ),
                ),
            ),
            market_state=market_state,
            bindings=(
                LedgerExposureBinding(
                    instrument_id=leg_ids[1],
                    quantity_unit="contract",
                    opened_at=_fixture_timestamp(opened, 41),
                ),
            ),
        ),
        higher_order_policy=HigherOrderRiskPolicy(
            policy_id=f"{profile_id}.higher.order",
            version="1",
            beta_multiplier=Decimal("1.10"),
            duration_fraction=Decimal("0.25"),
            vanna_multiplier=Decimal("0.50"),
            volga_multiplier=Decimal("0.75"),
            charm_multiplier=Decimal("0.25"),
            source_hash=source_hash("higher.order.policy"),
        ),
        offset_policy=OffsetPolicyV3(
            policy_id=f"{profile_id}.offset.policy",
            version="1",
        ),
    )

    observation_times = (
        _fixture_timestamp(opened, 1),
        _fixture_timestamp(opened, 2),
    )
    stress_observations = tuple(
        ScenarioFactorObservation(
            observation_id=f"{profile_id}.stress.observation.{index}",
            observed_at=observed_at,
            regime_id="reviewed",
            price_returns=((leg_ids[1], price_return),),
            source_hash=source_hash(f"stress.observation.{index}"),
        )
        for index, (observed_at, price_return) in enumerate(
            zip(
                observation_times,
                (Decimal("-0.10"), Decimal("-0.05")),
                strict=True,
            ),
            start=1,
        )
    )
    stress = PublicT04StressInputs(
        generation_spec=ScenarioPathGenerationSpec(
            path_id=f"{profile_id}.stress.path",
            mode=ScenarioPathMode.DETERMINISTIC,
            step_count=2,
            seed=1729,
            window_start=opened_text,
            window_end=_fixture_timestamp(opened, 5),
            regime_id="reviewed",
            model_hash=source_hash("stress.model"),
            block_length=1,
        ),
        observations=stress_observations,
        effective_times=(first_stress_at, second_stress_at),
        risk_limits=PathRiskLimits(
            maximum_drawdown_fraction=Decimal("0"),
            minimum_margin_surplus=Decimal("0"),
            minimum_liquidity_surplus=Decimal("0"),
        ),
        economic_policy=EconomicProjectionPolicy(
            policy_id=f"{profile_id}.economic.policy",
            version="1",
            maximum_absolute_basis_fraction=Decimal("1"),
            maximum_volatility_curvature=Decimal("1"),
            require_derivative_repricers=False,
            require_liquidation_costs_for_liquidity_shock=False,
        ),
    )
    return build_public_t04_inputs(
        profile_id=profile_id,
        opened_at=opened_text,
        closed_at=closed_text,
        provenance=provenance,
        research=research,
        execution=execution,
        accounting=accounting,
        risk=risk,
        stress=stress,
    )


__all__ = [
    "HigherOrderRiskPolicy",
    "PUBLIC_T04_SCHEMA_VERSION",
    "PublicExecutionAttempt",
    "PublicIntegratedProfileError",
    "PublicIntegratedProfileReceipt",
    "PublicInterLegMoveInput",
    "PublicLifecycleRule",
    "PublicProfileProvenanceKind",
    "PublicT04AccountingInputs",
    "PublicT04ExecutionInputs",
    "PublicFundingInput",
    "PublicT04Inputs",
    "PublicT04Provenance",
    "PublicT04ResearchInputs",
    "PublicT04RiskInputs",
    "PublicT04StressInputs",
    "build_public_t04_fixture_inputs",
    "build_public_t04_inputs",
    "run_public_integrated_profile",
]
