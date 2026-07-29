"""Source-owned public T-03 option conformance profile.

The public entry point in this module accepts immutable provider rows, listed
contracts, point-in-time market states, and valuation snapshots.  It does not
accept a volatility surface, supplier-owned Greeks as authority, a selected
contract, or a caller-created analytics mark.  Those are all derived here and
bound into one factory-only receipt.

The profile is intentionally bounded.  Trading-path contracts are European,
cash-settled, listed vanilla options.  American, futures-option, and arithmetic
Asian branches are exercised as derived model-conformance probes; they are not
silently treated as supported lifecycle products.
"""

from __future__ import annotations

import math
from dataclasses import InitVar, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from market_research.research.derivatives.common import (
    AvailabilityTimes,
    decimal_text,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from market_research.research.derivatives.options import (
    ExerciseStyle,
    FillStatus,
    OptionContract,
    OptionFill,
    OptionQuote,
    OptionSettlementInput,
    OptionType,
    SettlementType,
    TransactionSide,
    ValuationInputSnapshot,
    mark_option_position,
    position_from_fill,
    simulate_option_fill,
    simulate_option_lifecycle,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.market_state import (
    BorrowQuote,
    DividendYieldAssumption,
    MarketDataQuality,
    MarketState,
    ObservationMetadata,
    OptionContractQuote,
    OptionRight as MarketStateOptionRight,
    QuoteCondition,
    RateQuote,
    SpotQuote,
)
from market_research.research.multi_asset.option_analytics import (
    AnalyticsComparisonAction,
    AnalyticsComparisonPolicy,
    AmericanCrrBinomialModel,
    AmericanRichardsonBinomialModel,
    AsianArithmeticMonteCarloModel,
    AuthoritativeOptionAnalyticsFactory,
    AuthoritativeOptionAnalyticsReceipt,
    CalibratedVolatilitySurface,
    DayCountConvention,
    DeterministicProviderNormalizationAdapter,
    EuropeanBlackScholesModel,
    ForwardInput,
    ForwardMethod as AnalyticsForwardMethod,
    ForwardReceipt,
    FuturesBlack76Model,
    NormalizedOptionQuote,
    OptionAnalyticsError,
    OptionModelInput,
    OptionModelRegistry,
    OptionQuoteQualityCandidate,
    OptionQuoteQualityContext,
    OptionQuoteQualityPolicy,
    ProviderOptionQuoteRow,
    ProviderQuoteConvention,
    QualityScreenedOptionChain,
    RejectedSurfacePoint,
    SupplierAnalyticsObservation,
    SurfaceCalibrationPolicy,
    SurfaceCoordinate,
    SurfaceExtrapolation,
    SurfaceObservation,
    calibrate_volatility_surface,
    default_analytics_comparison_policy,
    default_option_quote_quality_policy,
    estimate_forward,
    screen_option_quote_quality,
    standard_provider_adapters,
    validate_option_model_conformance,
)
from market_research.research.multi_asset.option_path import (
    CalculatedOptionDelta,
    CleanedOptionChain,
    DEFAULT_OPTION_CLEANING_POLICY,
    DeltaFallback,
    ForwardEstimate,
    ForwardMethod as PathForwardMethod,
    OptionAttributionPolicy,
    OptionChainCleaner,
    OptionGreeks,
    OptionPathAttribution,
    OptionPathMark,
    OptionRight,
    OptionSelectionDecision,
    OptionSelectionPolicy,
    RawOptionObservation,
    attribute_option_path,
    select_option_contract,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    PortfolioSnapshot,
    UnifiedPortfolioLedger,
    adapt_option_fill,
    adapt_option_lifecycle,
    mark_event,
)


PUBLIC_OPTION_PROFILE_SCHEMA_VERSION = 1
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_RECEIPT_FACTORY_TOKEN = object()


class PublicOptionProfileError(ValueError):
    """The public T-03 input cannot support one authoritative interpretation."""


class PublicOptionInputProvenance(StrEnum):
    """Honest origin label carried into every public receipt."""

    EXTERNALLY_PREPARED_IMMUTABLE = "EXTERNALLY_PREPARED_IMMUTABLE"
    EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE = (
        "EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE"
    )


class PublicOptionLifecycleKind(StrEnum):
    """Lifecycle instructions recognized by the bounded public profile."""

    EXPIRY = "EXPIRY"
    EARLY_EXERCISE = "EARLY_EXERCISE"


def _hash(label: str, payload: object) -> str:
    return sha256_prefixed(payload, label=label)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
        or (positive and value <= _ZERO)
        or (non_negative and value < _ZERO)
    ):
        raise PublicOptionProfileError(f"{field_name}_invalid")
    return value


def _ordered_unique_hashes(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        require_hash(value, field_name)
        if value not in result:
            result.append(value)
    if not result:
        raise PublicOptionProfileError(f"{field_name}_required")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PublicOptionSelectionParameters:
    """Caller-visible economic objective; evidence bindings are added internally."""

    policy_id: str
    policy_version: str
    right: OptionRight
    target_days_to_expiry: int
    minimum_days_to_expiry: int
    maximum_days_to_expiry: int
    target_delta: Decimal
    maximum_delta_distance: Decimal
    minimum_liquidity_weight: Decimal
    fallback: DeltaFallback
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.policy_id, "public_option_selection.policy_id")
        require_stable_id(
            self.policy_version,
            "public_option_selection.policy_version",
        )
        if not (
            0
            <= self.minimum_days_to_expiry
            <= self.target_days_to_expiry
            <= self.maximum_days_to_expiry
        ):
            raise PublicOptionProfileError("public_option_selection_day_range_invalid")
        _decimal(self.target_delta, "public_option_selection.target_delta")
        if not -_ONE <= self.target_delta <= _ONE:
            raise PublicOptionProfileError(
                "public_option_selection_target_delta_invalid"
            )
        _decimal(
            self.maximum_delta_distance,
            "public_option_selection.maximum_delta_distance",
            non_negative=True,
        )
        _decimal(
            self.minimum_liquidity_weight,
            "public_option_selection.minimum_liquidity_weight",
            non_negative=True,
        )
        if self.minimum_liquidity_weight > _ONE:
            raise PublicOptionProfileError(
                "public_option_selection_liquidity_weight_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "public_t03_selection_parameters",
                {
                    "policy_id": self.policy_id,
                    "policy_version": self.policy_version,
                    "right": self.right.value,
                    "target_days_to_expiry": self.target_days_to_expiry,
                    "minimum_days_to_expiry": self.minimum_days_to_expiry,
                    "maximum_days_to_expiry": self.maximum_days_to_expiry,
                    "target_delta": decimal_text(self.target_delta),
                    "maximum_delta_distance": decimal_text(self.maximum_delta_distance),
                    "minimum_liquidity_weight": decimal_text(
                        self.minimum_liquidity_weight
                    ),
                    "fallback": self.fallback.value,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicOptionRepricingPrimitive:
    """One later immutable quote/state/valuation tuple for every candidate series."""

    market_state: MarketState
    valuation_input: ValuationInputSnapshot
    provider_row: ProviderOptionQuoteRow
    supplier_observation: SupplierAnalyticsObservation | None = None
    hedge_pnl: Decimal = _ZERO
    carry_pnl: Decimal = _ZERO
    slippage: Decimal = _ZERO
    transaction_cost: Decimal = _ZERO
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if parse_timestamp(
            self.market_state.valuation_at,
            "public_option_repricing.market_state_at",
        ) != parse_timestamp(
            self.valuation_input.valuation_at,
            "public_option_repricing.valuation_at",
        ):
            raise PublicOptionProfileError(
                "public_option_repricing_market_state_time_mismatch"
            )
        if self.provider_row.contract_id != self.valuation_input.contract.contract_id:
            raise PublicOptionProfileError("public_option_repricing_contract_mismatch")
        if (
            self.supplier_observation is not None
            and self.supplier_observation.contract_id != self.provider_row.contract_id
        ):
            raise PublicOptionProfileError(
                "public_option_repricing_supplier_contract_mismatch"
            )
        for field_name in ("hedge_pnl", "carry_pnl"):
            _decimal(
                getattr(self, field_name),
                f"public_option_repricing.{field_name}",
            )
        for field_name in ("slippage", "transaction_cost"):
            _decimal(
                getattr(self, field_name),
                f"public_option_repricing.{field_name}",
                non_negative=True,
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "public_t03_repricing_primitive",
                {
                    "market_state_hash": self.market_state.state_hash(),
                    "valuation_input_hash": self.valuation_input.content_hash,
                    "provider_row_hash": self.provider_row.raw_payload_hash,
                    "supplier_observation_hash": (
                        None
                        if self.supplier_observation is None
                        else self.supplier_observation.content_hash
                    ),
                    "hedge_pnl": decimal_text(self.hedge_pnl),
                    "carry_pnl": decimal_text(self.carry_pnl),
                    "slippage": decimal_text(self.slippage),
                    "transaction_cost": decimal_text(self.transaction_cost),
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicOptionLifecyclePrimitive:
    """Settlement observations for all candidates; selection happens later."""

    kind: PublicOptionLifecycleKind
    event_at: str
    settlement_inputs: tuple[OptionSettlementInput, ...]
    exercise_fraction: Decimal = _ONE
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        parse_timestamp(self.event_at, "public_option_lifecycle.event_at")
        settlements = tuple(
            sorted(self.settlement_inputs, key=lambda item: item.contract_id)
        )
        if not settlements or len({item.contract_id for item in settlements}) != len(
            settlements
        ):
            raise PublicOptionProfileError(
                "public_option_lifecycle_settlement_coverage_invalid"
            )
        object.__setattr__(self, "settlement_inputs", settlements)
        fraction = _decimal(
            self.exercise_fraction,
            "public_option_lifecycle.exercise_fraction",
            non_negative=True,
        )
        if fraction > _ONE:
            raise PublicOptionProfileError(
                "public_option_lifecycle_exercise_fraction_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "public_t03_lifecycle_primitive",
                {
                    "kind": self.kind.value,
                    "event_at": self.event_at,
                    "settlement_input_hashes": [
                        item.content_hash for item in settlements
                    ],
                    "exercise_fraction": decimal_text(fraction),
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicOptionProfileInput:
    """Canonical source inputs.  No derived analytics are admitted."""

    profile_input_id: str
    provenance: PublicOptionInputProvenance
    chain_id: str
    underlying_id: str
    market_state: MarketState
    contracts: tuple[OptionContract, ...]
    valuation_inputs: tuple[ValuationInputSnapshot, ...]
    provider_rows: tuple[ProviderOptionQuoteRow, ...]
    provider_conventions: tuple[ProviderQuoteConvention, ...]
    source_manifest_hashes: tuple[str, ...]
    repricing_inputs: tuple[PublicOptionRepricingPrimitive, ...]
    lifecycle: PublicOptionLifecyclePrimitive
    supplier_observations: tuple[SupplierAnalyticsObservation, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.profile_input_id, "public_option_input.profile_input_id")
        require_stable_id(self.chain_id, "public_option_input.chain_id")
        require_stable_id(self.underlying_id, "public_option_input.underlying_id")
        contracts = tuple(sorted(self.contracts, key=lambda item: item.contract_id))
        valuations = tuple(
            sorted(
                self.valuation_inputs,
                key=lambda item: item.contract.contract_id,
            )
        )
        rows = tuple(sorted(self.provider_rows, key=lambda item: item.contract_id))
        conventions = tuple(
            sorted(
                self.provider_conventions,
                key=lambda item: (item.provider_id, item.convention_version),
            )
        )
        if len(contracts) < 3:
            raise PublicOptionProfileError(
                "public_option_input_competing_contracts_insufficient"
            )
        contract_ids = [item.contract_id for item in contracts]
        if len(set(contract_ids)) != len(contract_ids):
            raise PublicOptionProfileError("public_option_input_contract_duplicate")
        if {item.contract.contract_id for item in valuations} != set(contract_ids) or {
            item.contract_id for item in rows
        } != set(contract_ids):
            raise PublicOptionProfileError(
                "public_option_input_contract_coverage_mismatch"
            )
        if any(item.underlying_id != self.underlying_id for item in contracts):
            raise PublicOptionProfileError("public_option_input_underlying_mismatch")
        if len({item.expiration_at for item in contracts}) != 1:
            raise PublicOptionProfileError(
                "public_option_input_single_expiry_profile_required"
            )
        if any(
            parse_timestamp(
                item.valuation_at,
                "public_option_input.valuation_at",
            )
            != parse_timestamp(
                self.market_state.valuation_at,
                "public_option_input.market_state_at",
            )
            for item in valuations
        ):
            raise PublicOptionProfileError(
                "public_option_input_valuation_time_mismatch"
            )
        convention_keys = [
            (item.provider_id, item.convention_version) for item in conventions
        ]
        if not conventions or len(convention_keys) != len(set(convention_keys)):
            raise PublicOptionProfileError(
                "public_option_input_provider_convention_duplicate"
            )
        convention_providers = {item.provider_id for item in conventions}
        if any(item.provider_id not in convention_providers for item in rows):
            raise PublicOptionProfileError(
                "public_option_input_provider_convention_missing"
            )
        suppliers = tuple(
            sorted(self.supplier_observations, key=lambda item: item.contract_id)
        )
        if len({item.contract_id for item in suppliers}) != len(suppliers):
            raise PublicOptionProfileError("public_option_input_supplier_duplicate")
        if any(item.contract_id not in set(contract_ids) for item in suppliers):
            raise PublicOptionProfileError(
                "public_option_input_supplier_contract_unknown"
            )
        repricings = tuple(
            sorted(
                self.repricing_inputs,
                key=lambda item: (
                    item.market_state.valuation_at,
                    item.provider_row.contract_id,
                ),
            )
        )
        if not repricings:
            raise PublicOptionProfileError("public_option_input_repricing_required")
        if any(
            item.provider_row.contract_id not in set(contract_ids)
            for item in repricings
        ):
            raise PublicOptionProfileError(
                "public_option_input_repricing_contract_unknown"
            )
        if {item.contract_id for item in self.lifecycle.settlement_inputs} != set(
            contract_ids
        ):
            raise PublicOptionProfileError(
                "public_option_input_lifecycle_contract_coverage_mismatch"
            )
        sources = _ordered_unique_hashes(
            self.source_manifest_hashes,
            "public_option_input.source_manifest_hash",
        )
        object.__setattr__(self, "contracts", contracts)
        object.__setattr__(self, "valuation_inputs", valuations)
        object.__setattr__(self, "provider_rows", rows)
        object.__setattr__(self, "provider_conventions", conventions)
        object.__setattr__(self, "supplier_observations", suppliers)
        object.__setattr__(self, "repricing_inputs", repricings)
        object.__setattr__(self, "source_manifest_hashes", sources)
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "public_t03_profile_input",
                {
                    "schema_version": PUBLIC_OPTION_PROFILE_SCHEMA_VERSION,
                    "profile_input_id": self.profile_input_id,
                    "provenance": self.provenance.value,
                    "chain_id": self.chain_id,
                    "underlying_id": self.underlying_id,
                    "market_state_hash": self.market_state.state_hash(),
                    "contract_hashes": [item.content_hash for item in contracts],
                    "valuation_input_hashes": [
                        item.content_hash for item in valuations
                    ],
                    "provider_row_hashes": [item.raw_payload_hash for item in rows],
                    "provider_convention_hashes": [
                        item.content_hash for item in conventions
                    ],
                    "source_manifest_hashes": list(sources),
                    "repricing_input_hashes": [
                        item.content_hash for item in repricings
                    ],
                    "lifecycle_hash": self.lifecycle.content_hash,
                    "supplier_observation_hashes": [
                        item.content_hash for item in suppliers
                    ],
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class _CarryBinding:
    state_hash: str
    spot: SpotQuote
    rate: RateQuote
    dividend: DividendYieldAssumption
    borrow: BorrowQuote
    forward: ForwardReceipt
    content_hash: str


@dataclass(frozen=True, slots=True)
class _BoundQuote:
    contract: OptionContract
    valuation: ValuationInputSnapshot
    normalized: NormalizedOptionQuote
    typed_quote: OptionContractQuote
    quality_candidate: OptionQuoteQualityCandidate
    carry: _CarryBinding


@dataclass(frozen=True, slots=True)
class PublicOptionInstitutionalReceipt:
    """Immutable, factory-only evidence for the complete public T-03 path."""

    receipt_id: str
    input_hash: str
    factory_hash: str
    provenance: PublicOptionInputProvenance
    raw_provider_row_hashes: tuple[str, ...]
    normalized_quote_hashes: tuple[str, ...]
    forward_receipt_hashes: tuple[str, ...]
    quality_chain: QualityScreenedOptionChain
    surface: CalibratedVolatilitySurface
    initial_analytics_receipts: tuple[AuthoritativeOptionAnalyticsReceipt, ...]
    selected_analytics_receipt: AuthoritativeOptionAnalyticsReceipt
    selection_decision: OptionSelectionDecision
    model_conformance_hashes: tuple[tuple[str, str], ...]
    repricing_analytics_receipts: tuple[AuthoritativeOptionAnalyticsReceipt, ...]
    repricing_quality_chain_hashes: tuple[str, ...]
    attribution: OptionPathAttribution
    fill: OptionFill
    position_mark_hashes: tuple[str, ...]
    lifecycle_hash: str
    ledger: UnifiedPortfolioLedger
    selected_contract_id: str
    executed_side: TransactionSide
    filled_quantity: Decimal
    ledger_event_count: int
    ledger_cash_balance: Decimal
    ledger_realized_pnl: Decimal
    attribution_actual_pnl: Decimal
    quality_flags: tuple[str, ...]
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _RECEIPT_FACTORY_TOKEN:
            raise PublicOptionProfileError(
                "public_option_receipt_requires_institutional_factory"
            )
        require_stable_id(self.receipt_id, "public_option_receipt.receipt_id")
        require_hash(self.input_hash, "public_option_receipt.input_hash")
        require_hash(self.factory_hash, "public_option_receipt.factory_hash")
        for field_name in (
            "raw_provider_row_hashes",
            "normalized_quote_hashes",
            "forward_receipt_hashes",
            "repricing_quality_chain_hashes",
            "position_mark_hashes",
        ):
            hashes = getattr(self, field_name)
            if not hashes:
                raise PublicOptionProfileError(
                    f"public_option_receipt.{field_name}_required"
                )
            for value in hashes:
                require_hash(
                    value,
                    f"public_option_receipt.{field_name}",
                )
        require_hash(
            self.lifecycle_hash,
            "public_option_receipt.lifecycle_hash",
        )
        if not self.model_conformance_hashes:
            raise PublicOptionProfileError(
                "public_option_receipt.model_conformance_hashes_required"
            )
        for name, value in self.model_conformance_hashes:
            require_stable_id(name, "public_option_receipt.model_conformance_name")
            require_hash(
                value,
                "public_option_receipt.model_conformance_hash",
            )
        if (
            self.selection_decision.selected_contract_id != self.selected_contract_id
            or self.selected_analytics_receipt.analytics_mark.contract_id
            != self.selected_contract_id
            or self.attribution.contract_id != self.selected_contract_id
            or self.fill.contract.contract_id != self.selected_contract_id
        ):
            raise PublicOptionProfileError(
                "public_option_receipt_selected_contract_binding_mismatch"
            )
        if not isinstance(self.executed_side, TransactionSide):
            raise PublicOptionProfileError(
                "public_option_receipt_executed_side_invalid"
            )
        if self.fill.side is not self.executed_side:
            raise PublicOptionProfileError(
                "public_option_receipt_executed_side_mismatch"
            )
        _decimal(
            self.filled_quantity,
            "public_option_receipt.filled_quantity",
            positive=True,
        )
        expected_position_quantity = (
            self.filled_quantity
            if self.executed_side is TransactionSide.BUY
            else -self.filled_quantity
        )
        if (
            self.fill.filled_quantity != self.filled_quantity
            or self.attribution.position_quantity != expected_position_quantity
        ):
            raise PublicOptionProfileError(
                "public_option_receipt_execution_quantity_binding_mismatch"
            )
        for field_name in (
            "ledger_cash_balance",
            "ledger_realized_pnl",
            "attribution_actual_pnl",
        ):
            _decimal(
                getattr(self, field_name),
                f"public_option_receipt.{field_name}",
            )
        if self.attribution.actual_pnl != self.attribution_actual_pnl:
            raise PublicOptionProfileError(
                "public_option_receipt_attribution_total_mismatch"
            )
        if self.ledger_event_count != len(self.ledger.events):
            raise PublicOptionProfileError(
                "public_option_receipt_ledger_event_count_mismatch"
            )
        flags = tuple(sorted(set(self.quality_flags)))
        if not flags:
            raise PublicOptionProfileError(
                "public_option_receipt_quality_flags_required"
            )
        for flag in flags:
            require_stable_id(flag, "public_option_receipt.quality_flag")
        object.__setattr__(self, "quality_flags", flags)
        object.__setattr__(
            self,
            "content_hash",
            _hash("public_t03_institutional_receipt", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": PUBLIC_OPTION_PROFILE_SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "input_hash": self.input_hash,
            "factory_hash": self.factory_hash,
            "provenance": self.provenance.value,
            "raw_provider_row_hashes": list(self.raw_provider_row_hashes),
            "normalized_quote_hashes": list(self.normalized_quote_hashes),
            "forward_receipt_hashes": list(self.forward_receipt_hashes),
            "quality_chain_hash": self.quality_chain.content_hash,
            "quality_record_hashes": [
                item.content_hash for item in self.quality_chain.records
            ],
            "surface_hash": self.surface.content_hash,
            "surface_pre_repair_diagnostic_hashes": [
                item.content_hash for item in self.surface.pre_repair_diagnostics
            ],
            "surface_post_repair_diagnostic_hashes": [
                item.content_hash for item in self.surface.post_repair_diagnostics
            ],
            "initial_analytics_receipt_hashes": [
                item.content_hash for item in self.initial_analytics_receipts
            ],
            "selected_analytics_receipt_hash": (
                self.selected_analytics_receipt.content_hash
            ),
            "selection_decision_hash": self.selection_decision.content_hash,
            "model_conformance_hashes": [
                {"name": name, "hash": value}
                for name, value in self.model_conformance_hashes
            ],
            "repricing_analytics_receipt_hashes": [
                item.content_hash for item in self.repricing_analytics_receipts
            ],
            "repricing_quality_chain_hashes": list(self.repricing_quality_chain_hashes),
            "attribution_hash": self.attribution.content_hash,
            "fill_hash": self.fill.content_hash,
            "position_mark_hashes": list(self.position_mark_hashes),
            "lifecycle_hash": self.lifecycle_hash,
            "ledger_hash": self.ledger.content_hash,
            "ledger_head_hash": self.ledger.head_hash,
            "selected_contract_id": self.selected_contract_id,
            "executed_side": self.executed_side.value,
            "filled_quantity": decimal_text(self.filled_quantity),
            "ledger_event_count": self.ledger_event_count,
            "ledger_cash_balance": decimal_text(self.ledger_cash_balance),
            "ledger_realized_pnl": decimal_text(self.ledger_realized_pnl),
            "attribution_actual_pnl": decimal_text(self.attribution_actual_pnl),
            "quality_flags": list(self.quality_flags),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def require_valid(self) -> None:
        for item in (
            *self.initial_analytics_receipts,
            self.selected_analytics_receipt,
            *self.repricing_analytics_receipts,
        ):
            item.require_valid()
        self.ledger.verify_integrity()
        if not self.attribution.reconciled:
            raise PublicOptionProfileError(
                "public_option_receipt_attribution_not_reconciled"
            )
        if self.content_hash != _hash(
            "public_t03_institutional_receipt",
            self.identity_payload(),
        ):
            raise PublicOptionProfileError("public_option_receipt_hash_mismatch")


@dataclass(frozen=True, slots=True)
class PublicOptionInstitutionalFactory:
    """Deterministic policy authority and sole creator of public receipts."""

    quote_quality_policy: OptionQuoteQualityPolicy
    surface_policy: SurfaceCalibrationPolicy
    selection_parameters: PublicOptionSelectionParameters
    attribution_policy: OptionAttributionPolicy
    model_registry: OptionModelRegistry
    comparison_policy: AnalyticsComparisonPolicy
    forward_policy_hash: str
    margin_model_hash: str
    margin_per_contract: Decimal
    collateral_per_contract: Decimal
    fill_quantity: Decimal
    fee_per_contract: Decimal = _ZERO
    slippage_ticks: int = 0
    day_count: DayCountConvention = DayCountConvention.ACT_365_25
    american_model_tolerance: Decimal = Decimal("2")
    factory_version: str = "public_t03_institutional_factory_v1"
    fill_side: TransactionSide = TransactionSide.BUY
    analytics_factory: AuthoritativeOptionAnalyticsFactory = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(
            self.forward_policy_hash,
            "public_option_factory.forward_policy_hash",
        )
        require_hash(
            self.margin_model_hash,
            "public_option_factory.margin_model_hash",
        )
        require_stable_id(
            self.factory_version,
            "public_option_factory.factory_version",
        )
        for field_name in (
            "margin_per_contract",
            "collateral_per_contract",
            "fee_per_contract",
        ):
            _decimal(
                getattr(self, field_name),
                f"public_option_factory.{field_name}",
                non_negative=True,
            )
        _decimal(
            self.fill_quantity,
            "public_option_factory.fill_quantity",
            positive=True,
        )
        if not isinstance(self.fill_side, TransactionSide):
            raise PublicOptionProfileError("public_option_factory.fill_side_invalid")
        _decimal(
            self.american_model_tolerance,
            "public_option_factory.american_model_tolerance",
            positive=True,
        )
        if (
            isinstance(self.slippage_ticks, bool)
            or not isinstance(self.slippage_ticks, int)
            or self.slippage_ticks < 0
        ):
            raise PublicOptionProfileError(
                "public_option_factory.slippage_ticks_invalid"
            )
        analytics_factory = AuthoritativeOptionAnalyticsFactory(
            registry=self.model_registry,
            comparison_policy=self.comparison_policy,
            margin_model_hash=self.margin_model_hash,
        )
        object.__setattr__(self, "analytics_factory", analytics_factory)
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "public_t03_institutional_factory",
                {
                    "schema_version": PUBLIC_OPTION_PROFILE_SCHEMA_VERSION,
                    "factory_version": self.factory_version,
                    "quote_quality_policy_hash": (
                        self.quote_quality_policy.content_hash
                    ),
                    "surface_policy_hash": self.surface_policy.content_hash,
                    "selection_parameters_hash": (
                        self.selection_parameters.content_hash
                    ),
                    "attribution_policy_hash": self.attribution_policy.content_hash,
                    "model_registry_hash": self.model_registry.content_hash,
                    "comparison_policy_hash": self.comparison_policy.content_hash,
                    "forward_policy_hash": self.forward_policy_hash,
                    "margin_model_hash": self.margin_model_hash,
                    "margin_per_contract": decimal_text(self.margin_per_contract),
                    "collateral_per_contract": decimal_text(
                        self.collateral_per_contract
                    ),
                    "fill_quantity": decimal_text(self.fill_quantity),
                    "fill_side": self.fill_side.value,
                    "fee_per_contract": decimal_text(self.fee_per_contract),
                    "slippage_ticks": self.slippage_ticks,
                    "day_count": self.day_count.value,
                    "american_model_tolerance": decimal_text(
                        self.american_model_tolerance
                    ),
                    "analytics_factory_hash": analytics_factory.content_hash,
                },
            ),
        )

    def _carry(
        self,
        *,
        state: MarketState,
        contract: OptionContract,
    ) -> _CarryBinding:
        state.require_usable()
        if parse_timestamp(
            state.valuation_at,
            "public_option_carry.valuation_at",
        ) >= parse_timestamp(
            contract.expiration_at,
            "public_option_carry.expiration_at",
        ):
            raise PublicOptionProfileError(
                "public_option_carry_contract_not_forward_dated"
            )
        spot = state.spot_price(contract.underlying_id)
        rate_candidates = [
            item for item in state.rates if item.currency == contract.currency
        ]
        if not rate_candidates:
            raise PublicOptionProfileError("public_option_carry_rate_missing")
        seconds = (
            parse_timestamp(contract.expiration_at, "option_contract.expiration_at")
            - parse_timestamp(state.valuation_at, "market_state.valuation_at")
        ).total_seconds()
        tenor_days = max(1, int(math.ceil(seconds / 86400)))
        rate = min(
            rate_candidates,
            key=lambda item: (
                abs(item.tenor_days - tenor_days),
                item.tenor_days,
                item.rate_id,
            ),
        )
        dividends = [
            item
            for item in state.dividend_yields
            if item.underlying_instrument_id == contract.underlying_id
        ]
        borrows = [
            item
            for item in state.borrow_quotes
            if item.instrument_id == contract.underlying_id
        ]
        if len(dividends) != 1 or len(borrows) != 1:
            raise PublicOptionProfileError(
                "public_option_carry_dividend_or_borrow_not_unique"
            )
        dividend = dividends[0]
        borrow = borrows[0]
        state_hash = state.state_hash()
        lineage = _ordered_unique_hashes(
            (
                state_hash,
                spot.metadata.source_hash,
                rate.metadata.source_hash,
                dividend.metadata.source_hash,
                dividend.model_hash,
                borrow.metadata.source_hash,
            ),
            "public_option_carry.input_hash",
        )
        forward = estimate_forward(
            ForwardInput(
                valuation_at=state.valuation_at,
                expiry_at=contract.expiration_at,
                spot_price=spot.price,
                futures_price=None,
                risk_free_rate=rate.rate,
                dividend_yield=dividend.annualized_yield,
                borrow_rate=borrow.annualized_rate,
                day_count=self.day_count,
                settlement_convention="PUBLIC_T03_LISTED_CASH_OPTION",
                discrete_dividends=(),
                market_state_hash=state_hash,
                policy_hash=self.forward_policy_hash,
                input_hashes=lineage,
            ),
            method=AnalyticsForwardMethod.SPOT_CARRY,
        )
        content_hash = _hash(
            "public_t03_carry_binding",
            {
                "state_hash": state_hash,
                "spot_source_hash": spot.metadata.source_hash,
                "rate_source_hash": rate.metadata.source_hash,
                "dividend_source_hash": dividend.metadata.source_hash,
                "borrow_source_hash": borrow.metadata.source_hash,
                "forward_hash": forward.content_hash,
            },
        )
        return _CarryBinding(
            state_hash=state_hash,
            spot=spot,
            rate=rate,
            dividend=dividend,
            borrow=borrow,
            forward=forward,
            content_hash=content_hash,
        )

    @staticmethod
    def _typed_quote(
        valuation: ValuationInputSnapshot,
        *,
        calendar_id: str,
    ) -> OptionContractQuote:
        source = valuation.quote
        if source.bid is None or source.ask is None:
            raise PublicOptionProfileError(
                "public_option_typed_quote_two_sided_required"
            )
        right = (
            MarketStateOptionRight.CALL
            if valuation.contract.option_type is OptionType.CALL
            else MarketStateOptionRight.PUT
        )
        return OptionContractQuote(
            contract_id=valuation.contract.contract_id,
            underlying_instrument_id=valuation.contract.underlying_id,
            expiry_at=valuation.contract.expiration_at,
            right=right,
            strike=valuation.contract.strike,
            currency=valuation.contract.currency,
            price_unit=f"{valuation.contract.currency}_per_underlying_unit",
            bid=source.bid,
            ask=source.ask,
            last=source.last,
            settlement=None,
            bid_size=source.bid_size,
            ask_size=source.ask_size,
            volume=Decimal(source.volume),
            open_interest=Decimal(source.open_interest),
            condition=QuoteCondition.NORMAL,
            metadata=ObservationMetadata(
                observed_at=source.availability.event_at,
                knowledge_at=source.availability.processed_at,
                source_hash=source.content_hash,
                calendar_id=calendar_id,
                max_age_seconds=source.stale_after_seconds,
                quality=MarketDataQuality.GOOD,
            ),
        )

    @staticmethod
    def _bind_valuation(
        *,
        contract: OptionContract,
        valuation: ValuationInputSnapshot,
        normalized: NormalizedOptionQuote,
        carry: _CarryBinding,
        source_manifest_hashes: tuple[str, ...],
    ) -> None:
        source = valuation.quote
        if valuation.contract != contract:
            raise PublicOptionProfileError("public_option_valuation_contract_mismatch")
        if (
            valuation.spot_price != carry.spot.price
            or valuation.risk_free_rate != carry.rate.rate
            or valuation.dividend_yield != carry.dividend.annualized_yield
            or valuation.forward_price != carry.forward.value
        ):
            raise PublicOptionProfileError(
                "public_option_valuation_carry_binding_mismatch"
            )
        if (
            source.contract_id != normalized.contract_id
            or source.bid != normalized.bid
            or source.ask != normalized.ask
            or source.bid_size != normalized.bid_size
            or source.ask_size != normalized.ask_size
            or source.volume != normalized.volume
            or source.open_interest != normalized.open_interest
        ):
            raise PublicOptionProfileError(
                "public_option_valuation_provider_quote_mismatch"
            )
        if (
            parse_timestamp(
                source.availability.event_at,
                "public_option_quote.event_at",
            )
            != parse_timestamp(
                normalized.observed_at_utc,
                "public_option_normalized.observed_at",
            )
            or parse_timestamp(
                source.availability.published_at,
                "public_option_quote.published_at",
            )
            != parse_timestamp(
                normalized.published_at_utc,
                "public_option_normalized.published_at",
            )
            or parse_timestamp(
                source.availability.processed_at,
                "public_option_quote.processed_at",
            )
            != parse_timestamp(
                normalized.available_at_utc,
                "public_option_normalized.available_at",
            )
        ):
            raise PublicOptionProfileError(
                "public_option_valuation_provider_clock_mismatch"
            )
        if normalized.bid is None or normalized.ask is None:
            raise PublicOptionProfileError(
                "public_option_valuation_normalized_quote_incomplete"
            )
        midpoint = (normalized.bid + normalized.ask) / _TWO
        if source.last is not None and source.last != midpoint:
            raise PublicOptionProfileError(
                "public_option_valuation_last_must_equal_source_midpoint"
            )
        admitted = set(source_manifest_hashes)
        if normalized.raw_row.source_artifact_hash not in admitted or not set(
            valuation.source_manifest_hashes
        ).issubset(admitted):
            raise PublicOptionProfileError(
                "public_option_valuation_source_manifest_mismatch"
            )

    def _bound_quote(
        self,
        *,
        state: MarketState,
        contract: OptionContract,
        valuation: ValuationInputSnapshot,
        row: ProviderOptionQuoteRow,
        adapter: DeterministicProviderNormalizationAdapter,
        source_manifest_hashes: tuple[str, ...],
    ) -> _BoundQuote:
        if (
            contract.exercise_style is not ExerciseStyle.EUROPEAN
            or contract.settlement_type is not SettlementType.CASH
        ):
            raise PublicOptionProfileError(
                "public_option_trading_contract_domain_unsupported"
            )
        normalized = adapter.normalize(
            row,
            contract_multiplier=contract.multiplier,
        )
        carry = self._carry(state=state, contract=contract)
        self._bind_valuation(
            contract=contract,
            valuation=valuation,
            normalized=normalized,
            carry=carry,
            source_manifest_hashes=source_manifest_hashes,
        )
        discount = Decimal(
            str(math.exp(-float(carry.rate.rate * carry.forward.time_years)))
        )
        context = OptionQuoteQualityContext(
            contract_id=contract.contract_id,
            underlying_id=contract.underlying_id,
            quote_underlying_id=contract.underlying_id,
            option_type=contract.option_type,
            exercise_style=contract.exercise_style.value,
            settlement_style=contract.settlement_type.value,
            expiry_at=contract.expiration_at,
            strike=contract.strike,
            spot_price=carry.spot.price,
            discount_factor=discount,
            decision_at=state.valuation_at,
            underlying_observed_at=carry.spot.metadata.observed_at,
            rate_observed_at=carry.rate.metadata.observed_at,
            dividend_observed_at=carry.dividend.metadata.observed_at,
            market_state_hash=carry.state_hash,
            underlying_hash=carry.spot.metadata.source_hash,
            rate_hash=carry.rate.metadata.source_hash,
            dividend_hash=carry.dividend.metadata.source_hash,
        )
        typed_quote = self._typed_quote(
            valuation,
            calendar_id=carry.spot.metadata.calendar_id,
        )
        return _BoundQuote(
            contract=contract,
            valuation=valuation,
            normalized=normalized,
            typed_quote=typed_quote,
            quality_candidate=OptionQuoteQualityCandidate(
                quote=normalized,
                context=context,
            ),
            carry=carry,
        )

    def _own_iv(
        self,
        valuation: ValuationInputSnapshot,
        price: Decimal,
    ) -> Decimal:
        result = self.analytics_factory.pricing_adapter.model.implied_volatility(
            valuation,
            price,
        )
        if not result.success or result.volatility is None:
            raise OptionAnalyticsError(
                "public_option_self_iv_failed:" + result.failure.value
            )
        return result.volatility

    def _model_conformance(
        self,
        *,
        valuation: ValuationInputSnapshot,
        implied_volatility: Decimal,
        source_hashes: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        contract = valuation.contract
        base = OptionModelInput(
            input_id=f"{valuation.valuation_input_id}.public-t03-european",
            contract_id=contract.contract_id,
            option_type=contract.option_type,
            exercise_style=ExerciseStyle.EUROPEAN,
            strike=contract.strike,
            time_years=valuation.time_to_expiry_years,
            spot=valuation.spot_price,
            forward=valuation.forward_price,
            volatility=implied_volatility,
            risk_free_rate=valuation.risk_free_rate,
            dividend_yield=valuation.dividend_yield,
            borrow_rate=_ZERO,
            payoff_kind="VANILLA",
            underlying_kind="SPOT",
            valuation_at=valuation.valuation_at,
            expiry_at=contract.expiration_at,
            day_count=self.day_count,
            source_hashes=source_hashes,
        )
        future = replace(
            base,
            input_id=f"{base.input_id}.future",
            contract_id=f"{contract.contract_id}.future-probe",
            underlying_kind="FUTURE",
        )
        american = replace(
            base,
            input_id=f"{base.input_id}.american",
            contract_id=f"{contract.contract_id}.american-put-probe",
            option_type=OptionType.PUT,
            exercise_style=ExerciseStyle.AMERICAN,
        )
        asian = replace(
            base,
            input_id=f"{base.input_id}.asian",
            contract_id=f"{contract.contract_id}.asian-probe",
            payoff_kind="ASIAN_ARITHMETIC",
            monitoring_steps=8,
        )
        evidence = validate_option_model_conformance(
            self.model_registry,
            european_spot_input=base,
            european_future_input=future,
            american_input=american,
            asian_input=asian,
            american_tolerance=self.american_model_tolerance,
        )
        return tuple(sorted(evidence.items()))

    def derive(
        self,
        inputs: PublicOptionProfileInput,
        *,
        receipt_id: str,
    ) -> PublicOptionInstitutionalReceipt:
        if not isinstance(inputs, PublicOptionProfileInput):
            raise PublicOptionProfileError("public_option_profile_input_required")
        if inputs.lifecycle.kind is not PublicOptionLifecycleKind.EXPIRY:
            raise PublicOptionProfileError("public_option_lifecycle_unsupported")
        inputs.market_state.require_usable()
        contracts = {item.contract_id: item for item in inputs.contracts}
        valuations = {
            item.contract.contract_id: item for item in inputs.valuation_inputs
        }
        rows = {item.contract_id: item for item in inputs.provider_rows}
        adapters = {
            item.provider_id: DeterministicProviderNormalizationAdapter(item)
            for item in inputs.provider_conventions
        }
        suppliers = {item.contract_id: item for item in inputs.supplier_observations}
        bound = tuple(
            self._bound_quote(
                state=inputs.market_state,
                contract=contracts[contract_id],
                valuation=valuations[contract_id],
                row=rows[contract_id],
                adapter=adapters[rows[contract_id].provider_id],
                source_manifest_hashes=inputs.source_manifest_hashes,
            )
            for contract_id in sorted(contracts)
        )
        quality_chain = screen_option_quote_quality(
            chain_id=inputs.chain_id,
            candidates=tuple(item.quality_candidate for item in bound),
            policy=self.quote_quality_policy,
        )
        included_ids = {item.contract_id for item in quality_chain.included_records}
        if len(included_ids) < 3:
            raise PublicOptionProfileError(
                "public_option_quality_included_contracts_insufficient"
            )
        bound_by_id = {item.contract.contract_id: item for item in bound}
        initial_receipts = tuple(
            self.analytics_factory.derive(
                receipt_id=f"{receipt_id}.initial.{contract_id}",
                quote=bound_by_id[contract_id].typed_quote,
                valuation_input=bound_by_id[contract_id].valuation,
                margin_per_contract=self.margin_per_contract,
                collateral_per_contract=self.collateral_per_contract,
                supplier_observation=suppliers.get(contract_id),
            )
            for contract_id in sorted(included_ids)
        )
        initial_by_id = {
            item.analytics_mark.contract_id: item for item in initial_receipts
        }
        observations: list[SurfaceObservation] = []
        for contract_id in sorted(included_ids):
            item = bound_by_id[contract_id]
            record = next(
                candidate
                for candidate in quality_chain.records
                if candidate.contract_id == contract_id
            )
            observations.append(
                SurfaceObservation(
                    contract_id=contract_id,
                    option_type=item.contract.option_type,
                    expiry_at=item.contract.expiration_at,
                    time_years=item.carry.forward.time_years,
                    strike=item.contract.strike,
                    spot=item.carry.spot.price,
                    forward=item.carry.forward.value,
                    discount_factor=item.quality_candidate.context.discount_factor,
                    raw_implied_volatility=(
                        initial_by_id[contract_id].market_implied_volatility
                    ),
                    bid_implied_volatility=self._own_iv(
                        item.valuation,
                        record.corrected_bid
                        if record.corrected_bid is not None
                        else item.typed_quote.bid,
                    ),
                    ask_implied_volatility=self._own_iv(
                        item.valuation,
                        record.corrected_ask
                        if record.corrected_ask is not None
                        else item.typed_quote.ask,
                    ),
                    delta=initial_by_id[contract_id].own_model_result.greeks.delta,
                    liquidity_weight=_ONE,
                    normalized_quote_hash=item.normalized.content_hash,
                    own_analytics_hash=initial_by_id[contract_id].content_hash,
                )
            )
        rejected = tuple(
            RejectedSurfacePoint(
                contract_id=item.contract_id,
                source_quote_hash=item.raw_quote.content_hash,
                quality_record_hash=item.content_hash,
                rejection_reasons=item.reasons,
            )
            for item in quality_chain.excluded_records
        )
        surface = calibrate_volatility_surface(
            surface_id=f"{inputs.chain_id}.public-t03-surface",
            calibrated_at=min(item.normalized.available_at_utc for item in bound),
            underlying_id=inputs.underlying_id,
            observations=tuple(observations),
            rejected_points=rejected,
            policy=self.surface_policy,
        )
        surfaced_receipts = tuple(
            self.analytics_factory.derive(
                receipt_id=f"{receipt_id}.surface.{contract_id}",
                quote=bound_by_id[contract_id].typed_quote,
                valuation_input=bound_by_id[contract_id].valuation,
                margin_per_contract=self.margin_per_contract,
                collateral_per_contract=self.collateral_per_contract,
                supplier_observation=suppliers.get(contract_id),
                surface=surface,
            )
            for contract_id in sorted(included_ids)
        )
        surfaced_by_id = {
            item.analytics_mark.contract_id: item for item in surfaced_receipts
        }
        first_carry = bound_by_id[sorted(included_ids)[0]].carry
        forward_estimate = ForwardEstimate(
            value=first_carry.forward.value,
            method=PathForwardMethod.SPOT_CARRY,
            estimated_at=parse_timestamp(
                inputs.market_state.valuation_at,
                "public_option.decision_at",
            ),
            input_hashes=(
                first_carry.forward.content_hash,
                first_carry.content_hash,
            ),
            rate=first_carry.rate.rate,
            dividend_yield=first_carry.dividend.annualized_yield,
            borrow_rate=first_carry.borrow.annualized_rate,
        )
        raw_path_observations = tuple(
            RawOptionObservation(
                contract_id=contract_id,
                underlying_id=inputs.underlying_id,
                right=(
                    OptionRight.CALL
                    if bound_by_id[contract_id].contract.option_type is OptionType.CALL
                    else OptionRight.PUT
                ),
                strike=bound_by_id[contract_id].contract.strike,
                expiry=parse_timestamp(
                    bound_by_id[contract_id].contract.expiration_at,
                    "public_option.contract.expiration_at",
                ),
                observed_at=parse_timestamp(
                    bound_by_id[contract_id].normalized.observed_at_utc,
                    "public_option.normalized.observed_at",
                ),
                known_at=parse_timestamp(
                    bound_by_id[contract_id].normalized.available_at_utc,
                    "public_option.normalized.available_at",
                ),
                bid=bound_by_id[contract_id].normalized.bid,
                ask=bound_by_id[contract_id].normalized.ask,
                bid_size=bound_by_id[contract_id].normalized.bid_size,
                ask_size=bound_by_id[contract_id].normalized.ask_size,
                volume=bound_by_id[contract_id].normalized.volume,
                open_interest=bound_by_id[contract_id].normalized.open_interest,
                bid_iv=next(
                    item.bid_implied_volatility
                    for item in observations
                    if item.contract_id == contract_id
                ),
                ask_iv=next(
                    item.ask_implied_volatility
                    for item in observations
                    if item.contract_id == contract_id
                ),
                delta=surfaced_by_id[contract_id].analytics_mark.delta,
                source_quote_hash=bound_by_id[contract_id].typed_quote.content_hash,
                adjusted_contract=bound_by_id[contract_id].contract.adjusted_contract,
            )
            for contract_id in sorted(included_ids)
        )
        cleaner = OptionChainCleaner(
            replace(
                DEFAULT_OPTION_CLEANING_POLICY,
                maximum_age_seconds=self.quote_quality_policy.maximum_age_seconds,
                maximum_relative_spread=(
                    self.quote_quality_policy.maximum_relative_spread
                ),
                minimum_quote_size=self.quote_quality_policy.minimum_quote_size,
                minimum_volume=self.quote_quality_policy.minimum_volume,
                minimum_open_interest=(self.quote_quality_policy.minimum_open_interest),
            )
        )
        cleaned_chain: CleanedOptionChain = cleaner.clean(
            underlying_id=inputs.underlying_id,
            decision_at=parse_timestamp(
                inputs.market_state.valuation_at,
                "public_option.decision_at",
            ),
            market_state_hash=inputs.market_state.state_hash(),
            spot=first_carry.spot.price,
            forward=forward_estimate,
            observations=raw_path_observations,
        )
        selection_policy = OptionSelectionPolicy(
            policy_id=self.selection_parameters.policy_id,
            version=self.selection_parameters.policy_version,
            right=self.selection_parameters.right,
            target_days_to_expiry=(self.selection_parameters.target_days_to_expiry),
            minimum_days_to_expiry=(self.selection_parameters.minimum_days_to_expiry),
            maximum_days_to_expiry=(self.selection_parameters.maximum_days_to_expiry),
            target_delta=self.selection_parameters.target_delta,
            maximum_delta_distance=(self.selection_parameters.maximum_delta_distance),
            minimum_liquidity_weight=(
                self.selection_parameters.minimum_liquidity_weight
            ),
            fallback=self.selection_parameters.fallback,
            model_specification_hash=(
                self.analytics_factory.pricing_adapter.specification.content_hash
            ),
        )
        decision_at = parse_timestamp(
            inputs.market_state.valuation_at,
            "public_option.decision_at",
        )
        calculated_deltas = tuple(
            CalculatedOptionDelta(
                contract_id=contract_id,
                calculated_at=decision_at,
                known_at=decision_at,
                delta=surfaced_by_id[contract_id].analytics_mark.delta,
                market_state_hash=inputs.market_state.state_hash(),
                model_specification_hash=selection_policy.model_specification_hash,
                valuation_input_hash=bound_by_id[contract_id].valuation.content_hash,
                source_quote_hash=bound_by_id[contract_id].typed_quote.content_hash,
                forward_hash=forward_estimate.content_hash,
            )
            for contract_id in sorted(included_ids)
        )
        selection = select_option_contract(
            cleaned_chain,
            selection_policy,
            calculated_deltas,
            {
                contract_id: bound_by_id[contract_id].valuation.content_hash
                for contract_id in sorted(included_ids)
            },
        )
        selected_id = selection.selected_contract_id
        if selected_id is None or len(selection.eligible_contract_ids) < 2:
            raise PublicOptionProfileError(
                "public_option_competing_contract_selection_failed"
            )
        selected_receipt = surfaced_by_id[selected_id]
        selected_bound = bound_by_id[selected_id]
        conformance = self._model_conformance(
            valuation=selected_bound.valuation,
            implied_volatility=selected_receipt.implied_volatility,
            source_hashes=(
                selected_bound.valuation.content_hash,
                selected_receipt.content_hash,
                surface.content_hash,
            ),
        )

        selected_repricing_inputs = tuple(
            item
            for item in inputs.repricing_inputs
            if item.provider_row.contract_id == selected_id
        )
        if not selected_repricing_inputs:
            raise PublicOptionProfileError(
                "public_option_selected_contract_repricing_missing"
            )
        repricing_receipts: list[AuthoritativeOptionAnalyticsReceipt] = []
        repricing_bounds: list[_BoundQuote] = []
        repricing_quality_hashes: list[str] = []
        for index, repricing_primitive in enumerate(selected_repricing_inputs):
            bound_repricing = self._bound_quote(
                state=repricing_primitive.market_state,
                contract=selected_bound.contract,
                valuation=repricing_primitive.valuation_input,
                row=repricing_primitive.provider_row,
                adapter=adapters[repricing_primitive.provider_row.provider_id],
                source_manifest_hashes=inputs.source_manifest_hashes,
            )
            repricing_quality = screen_option_quote_quality(
                chain_id=f"{inputs.chain_id}.repricing.{index}",
                candidates=(bound_repricing.quality_candidate,),
                policy=self.quote_quality_policy,
            )
            if not repricing_quality.records[0].included:
                raise PublicOptionProfileError(
                    "public_option_repricing_quality_rejected"
                )
            repricing_quality_hashes.append(repricing_quality.content_hash)
            repricing_bounds.append(bound_repricing)
            repricing_receipts.append(
                self.analytics_factory.derive(
                    receipt_id=f"{receipt_id}.repricing.{index}",
                    quote=bound_repricing.typed_quote,
                    valuation_input=bound_repricing.valuation,
                    margin_per_contract=self.margin_per_contract,
                    collateral_per_contract=self.collateral_per_contract,
                    supplier_observation=(repricing_primitive.supplier_observation),
                )
            )
        path_marks: list[OptionPathMark] = [
            _path_mark(
                selected_receipt,
                selected_bound,
            )
        ]
        for primitive, bound_repricing, repricing_receipt in zip(
            selected_repricing_inputs,
            repricing_bounds,
            repricing_receipts,
        ):
            path_marks.append(
                _path_mark(
                    repricing_receipt,
                    bound_repricing,
                    hedge_pnl=primitive.hedge_pnl,
                    carry_pnl=primitive.carry_pnl,
                    slippage=primitive.slippage,
                    transaction_cost=primitive.transaction_cost,
                )
            )
        signed_fill_quantity = (
            self.fill_quantity
            if self.fill_side is TransactionSide.BUY
            else -self.fill_quantity
        )
        attribution = attribute_option_path(
            path_marks,
            signed_quantity=signed_fill_quantity,
            multiplier=selected_bound.contract.multiplier,
            policy=self.attribution_policy,
        )
        fill = simulate_option_fill(
            fill_id=f"{receipt_id}.fill",
            contract=selected_bound.contract,
            quote=selected_bound.valuation.quote,
            side=self.fill_side,
            quantity=self.fill_quantity,
            filled_at=selected_bound.valuation.valuation_at,
            fee_per_contract=self.fee_per_contract,
            slippage_ticks=self.slippage_ticks,
        )
        if fill.status is not FillStatus.FILLED:
            raise PublicOptionProfileError("public_option_execution_not_filled")
        position = position_from_fill(
            fill,
            position_id=f"{receipt_id}.position",
        )
        derivative_marks = [
            mark_option_position(
                position,
                quote=selected_bound.valuation.quote,
                theoretical_price=selected_receipt.analytics_mark.model_price,
                theoretical_input_hash=selected_receipt.content_hash,
                marked_at=selected_bound.valuation.valuation_at,
            )
        ]
        for bound_repricing, repricing_receipt in zip(
            repricing_bounds,
            repricing_receipts,
        ):
            derivative_marks.append(
                mark_option_position(
                    position,
                    quote=bound_repricing.valuation.quote,
                    theoretical_price=(repricing_receipt.analytics_mark.model_price),
                    theoretical_input_hash=repricing_receipt.content_hash,
                    marked_at=bound_repricing.valuation.valuation_at,
                )
            )
        settlements = {
            item.contract_id: item for item in inputs.lifecycle.settlement_inputs
        }
        lifecycle_event = simulate_option_lifecycle(
            position,
            event_id=f"{receipt_id}.lifecycle",
            event_at=inputs.lifecycle.event_at,
            settlement_input=settlements[selected_id],
            exercise_fraction=inputs.lifecycle.exercise_fraction,
        )
        ledger = UnifiedPortfolioLedger.open(
            ledger_id=f"{receipt_id}.ledger",
            base_currency=selected_bound.contract.currency,
        ).publish_many(
            adapt_option_fill(
                fill,  # type: ignore[arg-type]
                execution_context_hash=selection.content_hash,
            )
        )
        for index, mark in enumerate(path_marks):
            ledger = ledger.publish(
                mark_event(
                    event_id=f"{receipt_id}.mark.{index}",
                    occurred_at=_utc_text(mark.marked_at),
                    asset_class=AssetClass.OPTION,
                    instrument_id=selected_id,
                    currency=selected_bound.contract.currency,
                    mark_price=mark.market_price,
                    source_hashes=(mark.content_hash,),
                )
            )
        ledger = ledger.publish(
            adapt_option_lifecycle(lifecycle_event, position=position)
        )
        snapshot = ledger.replay()
        if any(item.instrument_id == selected_id for item in snapshot.positions):
            raise PublicOptionProfileError(
                "public_option_lifecycle_position_not_closed"
            )
        quality_flags = {
            f"INPUT_PROVENANCE_{inputs.provenance.value}",
            "RAW_PROVIDER_ROWS_RETAINED",
            "NORMALIZATION_SOURCE_BOUND",
            "QUOTE_QUALITY_SCREENED",
            "SELF_IV_AND_GREEKS_DERIVED",
            "FORWARD_BOUND_TO_MARKET_STATE_AND_POLICY",
            "AMERICAN_MODEL_BRANCH_VALIDATED",
            "ASIAN_EXOTIC_MODEL_BRANCH_VALIDATED",
            "CONTRACT_SELECTED_FROM_COMPETING_CHAIN",
            "INTERMEDIATE_REPRICING_BOUND",
            "ATTRIBUTION_RECONCILED",
            "LIFECYCLE_LEDGER_RECONCILED",
        }
        quality_flags.add(
            "STATIC_ARBITRAGE_REPAIRED"
            if surface.repair_count
            else "STATIC_ARBITRAGE_PASSED_RAW"
        )
        quality_flags.add(
            "SUPPLIER_ANALYTICS_" + selected_receipt.supplier_comparison.status.value
        )
        if quality_chain.modified_records:
            quality_flags.add("QUOTE_QUALITY_MODIFICATIONS_RETAINED")
        if quality_chain.excluded_records:
            quality_flags.add("QUOTE_QUALITY_EXCLUSIONS_RETAINED")
        receipt = PublicOptionInstitutionalReceipt(
            receipt_id=receipt_id,
            input_hash=inputs.content_hash,
            factory_hash=self.content_hash,
            provenance=inputs.provenance,
            raw_provider_row_hashes=tuple(
                item.raw_payload_hash
                for item in (
                    *inputs.provider_rows,
                    *(item.provider_row for item in inputs.repricing_inputs),
                )
            ),
            normalized_quote_hashes=tuple(
                item.normalized.content_hash for item in (*bound, *repricing_bounds)
            ),
            forward_receipt_hashes=tuple(
                item.carry.forward.content_hash for item in (*bound, *repricing_bounds)
            ),
            quality_chain=quality_chain,
            surface=surface,
            initial_analytics_receipts=initial_receipts,
            selected_analytics_receipt=selected_receipt,
            selection_decision=selection,
            model_conformance_hashes=conformance,
            repricing_analytics_receipts=tuple(repricing_receipts),
            repricing_quality_chain_hashes=tuple(repricing_quality_hashes),
            attribution=attribution,
            fill=fill,
            position_mark_hashes=tuple(item.content_hash for item in derivative_marks),
            lifecycle_hash=lifecycle_event.content_hash,
            ledger=ledger,
            selected_contract_id=selected_id,
            executed_side=fill.side,
            filled_quantity=fill.filled_quantity,
            ledger_event_count=len(ledger.events),
            ledger_cash_balance=_snapshot_balance(
                snapshot,
                selected_bound.contract.currency,
                realized=False,
            ),
            ledger_realized_pnl=_snapshot_balance(
                snapshot,
                selected_bound.contract.currency,
                realized=True,
            ),
            attribution_actual_pnl=attribution.actual_pnl,
            quality_flags=tuple(quality_flags),
            _factory_token=_RECEIPT_FACTORY_TOKEN,
        )
        receipt.require_valid()
        return receipt


def _path_mark(
    receipt: AuthoritativeOptionAnalyticsReceipt,
    bound: _BoundQuote,
    *,
    hedge_pnl: Decimal = _ZERO,
    carry_pnl: Decimal = _ZERO,
    slippage: Decimal = _ZERO,
    transaction_cost: Decimal = _ZERO,
) -> OptionPathMark:
    greeks = receipt.own_model_result.greeks
    return OptionPathMark(
        contract_id=bound.contract.contract_id,
        marked_at=parse_timestamp(
            bound.valuation.valuation_at,
            "public_option_path.marked_at",
        ),
        market_state_hash=bound.carry.state_hash,
        market_quote_hash=bound.typed_quote.content_hash,
        model_specification_hash=(receipt.analytics_mark.model_specification_hash),
        market_price=receipt.analytics_mark.market_price,
        theoretical_price=receipt.analytics_mark.model_price,
        spot_price=bound.valuation.spot_price,
        implied_volatility=receipt.implied_volatility,
        rate=bound.valuation.risk_free_rate,
        dividend_yield=bound.valuation.dividend_yield,
        skew=_ZERO,
        greeks=OptionGreeks(
            delta=greeks.delta,
            gamma=greeks.gamma,
            vega_per_vol_point=greeks.vega_per_vol_point,
            theta_per_calendar_day=greeks.theta_per_calendar_day,
            rho_per_rate_point=greeks.rho_per_rate_point,
            vanna=greeks.vanna,
            volga=greeks.volga,
            charm=greeks.charm,
        ),
        hedge_pnl_since_previous=hedge_pnl,
        carry_pnl_since_previous=carry_pnl,
        slippage_since_previous=slippage,
        transaction_cost_since_previous=transaction_cost,
    )


def _snapshot_balance(
    snapshot: PortfolioSnapshot,
    currency: str,
    *,
    realized: bool,
) -> Decimal:
    balances = snapshot.realized_pnl if realized else snapshot.cash
    return sum(
        (item.amount for item in balances if item.currency == currency),
        _ZERO,
    )


def build_public_t03_inputs(
    *,
    profile_input_id: str,
    provenance: PublicOptionInputProvenance,
    chain_id: str,
    underlying_id: str,
    market_state: MarketState,
    contracts: Sequence[OptionContract],
    valuation_inputs: Sequence[ValuationInputSnapshot],
    provider_rows: Sequence[ProviderOptionQuoteRow],
    provider_conventions: Sequence[ProviderQuoteConvention],
    source_manifest_hashes: Sequence[str],
    repricing_inputs: Sequence[PublicOptionRepricingPrimitive],
    lifecycle: PublicOptionLifecyclePrimitive,
    supplier_observations: Sequence[SupplierAnalyticsObservation] = (),
) -> PublicOptionProfileInput:
    """Build canonical public inputs without accepting any derived authority."""

    return PublicOptionProfileInput(
        profile_input_id=profile_input_id,
        provenance=provenance,
        chain_id=chain_id,
        underlying_id=underlying_id,
        market_state=market_state,
        contracts=tuple(contracts),
        valuation_inputs=tuple(valuation_inputs),
        provider_rows=tuple(provider_rows),
        provider_conventions=tuple(provider_conventions),
        source_manifest_hashes=tuple(source_manifest_hashes),
        repricing_inputs=tuple(repricing_inputs),
        lifecycle=lifecycle,
        supplier_observations=tuple(supplier_observations),
    )


def run_public_option_profile(
    *,
    receipt_id: str,
    inputs: PublicOptionProfileInput,
    factory: PublicOptionInstitutionalFactory,
) -> PublicOptionInstitutionalReceipt:
    """Stable service entry point used by public adapters."""

    return factory.derive(inputs, receipt_id=receipt_id)


def default_public_option_institutional_factory(
    *,
    fill_quantity: Decimal = Decimal("1"),
    fill_side: TransactionSide = TransactionSide.BUY,
    comparison_action: AnalyticsComparisonAction = AnalyticsComparisonAction.REJECT,
) -> PublicOptionInstitutionalFactory:
    """Return the bounded deterministic policy set used by the public fixture."""

    registry = OptionModelRegistry(
        (
            EuropeanBlackScholesModel(),
            FuturesBlack76Model(),
            AmericanCrrBinomialModel(
                steps=100,
                convergence_tolerance=Decimal("2"),
            ),
            AmericanRichardsonBinomialModel(
                coarse_steps=60,
                convergence_tolerance=Decimal("2"),
            ),
            AsianArithmeticMonteCarloModel(
                paths=512,
                seed=1729,
                convergence_tolerance=Decimal("5"),
            ),
        ),
        registry_version="public_t03_model_registry_v1",
    )
    return PublicOptionInstitutionalFactory(
        quote_quality_policy=default_option_quote_quality_policy(),
        surface_policy=SurfaceCalibrationPolicy(
            policy_id="public_t03_static_arbitrage_projection",
            policy_version="v1",
            coordinate=SurfaceCoordinate.STRIKE,
            extrapolation=SurfaceExtrapolation.REJECT,
            maximum_repair_price_residual=Decimal("100"),
        ),
        selection_parameters=PublicOptionSelectionParameters(
            policy_id="public_t03_competing_contract_selection",
            policy_version="v1",
            right=OptionRight.CALL,
            target_days_to_expiry=180,
            minimum_days_to_expiry=30,
            maximum_days_to_expiry=365,
            target_delta=Decimal("0.50"),
            maximum_delta_distance=Decimal("1"),
            minimum_liquidity_weight=Decimal("0.25"),
            fallback=DeltaFallback.NEAREST_WITH_EVIDENCE,
        ),
        attribution_policy=OptionAttributionPolicy(
            policy_id="public_t03_path_attribution",
            version="v1",
            maximum_absolute_residual=Decimal("1000"),
            maximum_relative_residual=Decimal("1"),
        ),
        model_registry=registry,
        comparison_policy=default_analytics_comparison_policy(action=comparison_action),
        forward_policy_hash=_hash("public_t03_policy", "spot-carry-act-365.25-v1"),
        margin_model_hash=_hash("public_t03_policy", "bounded-margin-v1"),
        margin_per_contract=Decimal("250"),
        collateral_per_contract=Decimal("500"),
        fill_quantity=fill_quantity,
        fill_side=fill_side,
        fee_per_contract=Decimal("1"),
        slippage_ticks=0,
    )


def _fixture_metadata(
    *,
    observed_at: str,
    knowledge_at: str,
    valuation_at: str,
    source_hash: str,
    calendar_id: str,
) -> ObservationMetadata:
    age = int(
        (
            parse_timestamp(valuation_at, "fixture.valuation_at")
            - parse_timestamp(observed_at, "fixture.observed_at")
        ).total_seconds()
    )
    return ObservationMetadata(
        observed_at=observed_at,
        knowledge_at=knowledge_at,
        source_hash=source_hash,
        calendar_id=calendar_id,
        max_age_seconds=max(60, age + 60),
    )


def _fixture_availability(
    *,
    observed_at: str,
    knowledge_at: str,
) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=observed_at,
        published_at=knowledge_at,
        provider_received_at=knowledge_at,
        system_received_at=knowledge_at,
        processed_at=knowledge_at,
    )


def _fixture_market_state(
    *,
    state_id: str,
    valuation_at: str,
    observed_at: str,
    knowledge_at: str,
    source_hash: str,
    underlying_id: str,
    spot_price: Decimal,
    currency: str = "USD",
) -> MarketState:
    calendar_id = "calendar.public.t03"
    metadata = _fixture_metadata(
        observed_at=observed_at,
        knowledge_at=knowledge_at,
        valuation_at=valuation_at,
        source_hash=source_hash,
        calendar_id=calendar_id,
    )
    return MarketState(
        state_id=state_id,
        valuation_at=valuation_at,
        base_currency=currency,
        calendar_ids=(calendar_id,),
        spots=(
            SpotQuote(
                instrument_id=underlying_id,
                price=spot_price,
                currency=currency,
                unit=f"{currency}_per_share",
                metadata=metadata,
            ),
        ),
        rates=(
            RateQuote(
                rate_id=f"{state_id}.rate",
                currency=currency,
                tenor_days=180,
                rate=_ZERO,
                metadata=metadata,
            ),
        ),
        borrow_quotes=(
            BorrowQuote(
                instrument_id=underlying_id,
                currency=currency,
                annualized_rate=_ZERO,
                metadata=metadata,
            ),
        ),
        dividend_yields=(
            DividendYieldAssumption(
                assumption_id=f"{state_id}.dividend",
                underlying_instrument_id=underlying_id,
                currency=currency,
                annualized_yield=_ZERO,
                model_hash=_hash("public_t03_fixture", f"{state_id}.dividend"),
                metadata=metadata,
            ),
        ),
    )


def _fixture_provider_row(
    *,
    provider_record_id: str,
    contract_id: str,
    observed_at: str,
    knowledge_at: str,
    bid: Decimal,
    ask: Decimal,
    source_hash: str,
    schema_hash: str,
) -> ProviderOptionQuoteRow:
    return ProviderOptionQuoteRow(
        provider_id="provider_unit_utc",
        provider_record_id=provider_record_id,
        contract_id=contract_id,
        observed_at=observed_at,
        published_at=knowledge_at,
        available_at=knowledge_at,
        bid=bid,
        ask=ask,
        bid_size=Decimal("20"),
        ask_size=Decimal("20"),
        volume=200,
        open_interest=1000,
        source_artifact_hash=source_hash,
        provider_schema_hash=schema_hash,
    )


def _fixture_valuation(
    *,
    valuation_input_id: str,
    contract: OptionContract,
    row: ProviderOptionQuoteRow,
    valuation_at: str,
    spot_price: Decimal,
    source_manifest_hashes: tuple[str, ...],
) -> ValuationInputSnapshot:
    availability = _fixture_availability(
        observed_at=row.observed_at,
        knowledge_at=row.available_at,
    )
    quote = OptionQuote(
        quote_id=f"{valuation_input_id}.quote",
        contract_id=contract.contract_id,
        availability=availability,
        as_of=valuation_at,
        bid=row.bid,
        ask=row.ask,
        last=(
            None if row.bid is None or row.ask is None else (row.bid + row.ask) / _TWO
        ),
        bid_size=row.bid_size,
        ask_size=row.ask_size,
        volume=row.volume,
        open_interest=row.open_interest,
        stale_after_seconds=max(
            60,
            int(
                (
                    parse_timestamp(valuation_at, "fixture.valuation_at")
                    - parse_timestamp(row.observed_at, "fixture.observed_at")
                ).total_seconds()
            )
            + 60,
        ),
        min_volume=1,
        min_open_interest=1,
    )
    return ValuationInputSnapshot(
        valuation_input_id=valuation_input_id,
        contract=contract,
        quote=quote,
        valuation_at=valuation_at,
        spot_price=spot_price,
        risk_free_rate=_ZERO,
        dividend_yield=_ZERO,
        forward_price=spot_price,
        spot_availability=availability,
        rate_availability=availability,
        dividend_availability=availability,
        forward_availability=availability,
        source_manifest_hashes=source_manifest_hashes,
    )


def build_public_t03_fixture_inputs(
    *,
    source_document_id: str,
    source_document_hashes: Sequence[str],
    observation_at: str,
    valuation_at: str,
    knowledge_at: str,
    underlying_id: str = "underlying.public.t03",
    contract_ids: tuple[str, ...] = (
        "option.public.t03.call.90",
        "option.public.t03.call.100",
        "option.public.t03.call.110",
    ),
    strikes: tuple[Decimal, ...] = (
        Decimal("90"),
        Decimal("100"),
        Decimal("110"),
    ),
    quotes: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal("11.9"), Decimal("12.1")),
        (Decimal("9.9"), Decimal("10.1")),
        (Decimal("3.9"), Decimal("4.1")),
    ),
    spot_price: Decimal = Decimal("100"),
    quantity: Decimal = Decimal("1"),
    repricing_observation_at: str | None = None,
    repricing_knowledge_at: str | None = None,
    repricing_valuation_at: str | None = None,
    repricing_quotes: tuple[tuple[Decimal, Decimal], ...] | None = None,
    repricing_spot_price: Decimal | None = None,
    settlement_observation_at: str | None = None,
    settlement_knowledge_at: str | None = None,
    settlement_spot_price: Decimal | None = None,
    lifecycle_event_at: str | None = None,
    contract_expiration_at: str | None = None,
    contract_settlement_at: str | None = None,
    contract_multiplier: Decimal | None = None,
    contract_currency: str | None = None,
) -> PublicOptionProfileInput:
    """Create an honestly labelled, source-bound synthetic T-03 conformance input.

    The fixture is suitable for a public reproducibility probe.  It contains
    raw immutable rows and competing contracts but deliberately contains no
    analytics, surface, delta selection, or lifecycle result.

    Optional repricing, settlement, lifecycle, and contract fields admit exact
    immutable source primitives.  An omitted field retains the original
    deterministic conformance fallback.
    """

    require_stable_id(source_document_id, "public_t03_fixture.source_document_id")
    require_stable_id(underlying_id, "public_t03_fixture.underlying_id")
    sources = _ordered_unique_hashes(
        tuple(source_document_hashes),
        "public_t03_fixture.source_document_hash",
    )
    if (
        len(contract_ids) < 2
        or len(set(contract_ids)) != len(contract_ids)
        or len(strikes) != len(contract_ids)
        or len(quotes) != len(contract_ids)
    ):
        raise PublicOptionProfileError(
            "public_t03_fixture_competing_contract_grid_invalid"
        )
    for value in strikes:
        _decimal(value, "public_t03_fixture.strike", positive=True)
    for bid, ask in quotes:
        _decimal(bid, "public_t03_fixture.bid", positive=True)
        _decimal(ask, "public_t03_fixture.ask", positive=True)
        if ask < bid:
            raise PublicOptionProfileError("public_t03_fixture_quote_crossed")
    _decimal(spot_price, "public_t03_fixture.spot_price", positive=True)
    _decimal(quantity, "public_t03_fixture.quantity", positive=True)
    resolved_multiplier = (
        Decimal("100")
        if contract_multiplier is None
        else _decimal(
            contract_multiplier,
            "public_t03_fixture.contract_multiplier",
            positive=True,
        )
    )
    resolved_currency = "USD" if contract_currency is None else contract_currency
    if (
        not isinstance(resolved_currency, str)
        or len(resolved_currency) != 3
        or not resolved_currency.isalpha()
        or resolved_currency != resolved_currency.upper()
    ):
        raise PublicOptionProfileError("public_t03_fixture_contract_currency_invalid")
    observed = parse_timestamp(observation_at, "public_t03_fixture.observation_at")
    known = parse_timestamp(knowledge_at, "public_t03_fixture.knowledge_at")
    valuation = parse_timestamp(valuation_at, "public_t03_fixture.valuation_at")
    if not observed <= known <= valuation:
        raise PublicOptionProfileError("public_t03_fixture_clock_order_invalid")
    expiry = (
        valuation + timedelta(days=180)
        if contract_expiration_at is None
        else parse_timestamp(
            contract_expiration_at,
            "public_t03_fixture.contract_expiration_at",
        )
    )
    settlement = (
        expiry + timedelta(hours=1)
        if contract_settlement_at is None
        else parse_timestamp(
            contract_settlement_at,
            "public_t03_fixture.contract_settlement_at",
        )
    )
    if not valuation < expiry <= settlement:
        raise PublicOptionProfileError(
            "public_t03_fixture_contract_clock_order_invalid"
        )
    listing = valuation - timedelta(days=30)
    resolved_repricing_valuation = (
        valuation + timedelta(days=1)
        if repricing_valuation_at is None
        else parse_timestamp(
            repricing_valuation_at,
            "public_t03_fixture.repricing_valuation_at",
        )
    )
    resolved_repricing_observed = (
        resolved_repricing_valuation - timedelta(seconds=10)
        if repricing_observation_at is None
        else parse_timestamp(
            repricing_observation_at,
            "public_t03_fixture.repricing_observation_at",
        )
    )
    resolved_repricing_known = (
        resolved_repricing_valuation - timedelta(seconds=2)
        if repricing_knowledge_at is None
        else parse_timestamp(
            repricing_knowledge_at,
            "public_t03_fixture.repricing_knowledge_at",
        )
    )
    if not (
        valuation < resolved_repricing_valuation < expiry
        and resolved_repricing_observed
        <= resolved_repricing_known
        <= resolved_repricing_valuation
    ):
        raise PublicOptionProfileError(
            "public_t03_fixture_repricing_clock_order_invalid"
        )
    resolved_repricing_quotes = (
        tuple((bid + Decimal("0.4"), ask + Decimal("0.4")) for bid, ask in quotes)
        if repricing_quotes is None
        else tuple(repricing_quotes)
    )
    if len(resolved_repricing_quotes) != len(contract_ids):
        raise PublicOptionProfileError(
            "public_t03_fixture_repricing_quote_grid_invalid"
        )
    for bid, ask in resolved_repricing_quotes:
        _decimal(bid, "public_t03_fixture.repricing_bid", positive=True)
        _decimal(ask, "public_t03_fixture.repricing_ask", positive=True)
        if ask < bid:
            raise PublicOptionProfileError("public_t03_fixture_repricing_quote_crossed")
    resolved_repricing_spot = (
        spot_price + Decimal("1")
        if repricing_spot_price is None
        else _decimal(
            repricing_spot_price,
            "public_t03_fixture.repricing_spot_price",
            positive=True,
        )
    )
    resolved_settlement_observed = (
        expiry
        if settlement_observation_at is None
        else parse_timestamp(
            settlement_observation_at,
            "public_t03_fixture.settlement_observation_at",
        )
    )
    resolved_settlement_known = (
        resolved_settlement_observed + timedelta(seconds=10)
        if settlement_knowledge_at is None
        else parse_timestamp(
            settlement_knowledge_at,
            "public_t03_fixture.settlement_knowledge_at",
        )
    )
    resolved_lifecycle_event = (
        max(settlement, resolved_settlement_known)
        if lifecycle_event_at is None
        else parse_timestamp(
            lifecycle_event_at,
            "public_t03_fixture.lifecycle_event_at",
        )
    )
    if not (
        expiry <= resolved_settlement_observed <= settlement
        and resolved_settlement_observed
        <= resolved_settlement_known
        <= resolved_lifecycle_event
    ):
        raise PublicOptionProfileError(
            "public_t03_fixture_settlement_clock_order_invalid"
        )
    resolved_settlement_spot = (
        spot_price + Decimal("5")
        if settlement_spot_price is None
        else _decimal(
            settlement_spot_price,
            "public_t03_fixture.settlement_spot_price",
            positive=True,
        )
    )
    source_hash = sources[0]
    schema_hash = _hash(
        "public_t03_fixture_provider_schema",
        {
            "source_document_id": source_document_id,
            "source_document_hashes": list(sources),
        },
    )
    admitted_sources = _ordered_unique_hashes(
        (*sources, schema_hash),
        "public_t03_fixture.admitted_source_hash",
    )
    state = _fixture_market_state(
        state_id=f"{source_document_id}.t03.decision",
        valuation_at=_utc_text(valuation),
        observed_at=_utc_text(observed),
        knowledge_at=_utc_text(known),
        source_hash=source_hash,
        underlying_id=underlying_id,
        spot_price=spot_price,
        currency=resolved_currency,
    )
    contracts = tuple(
        OptionContract(
            contract_id=contract_id,
            underlying_id=underlying_id,
            option_type=OptionType.CALL,
            strike=strike,
            expiration_at=_utc_text(expiry),
            exercise_style=ExerciseStyle.EUROPEAN,
            settlement_type=SettlementType.CASH,
            multiplier=resolved_multiplier,
            currency=resolved_currency,
            exchange="exchange.public.t03",
            listing_at=_utc_text(listing),
            last_trade_at=_utc_text(expiry),
            settlement_at=_utc_text(settlement),
            price_tick=Decimal("0.01"),
        )
        for contract_id, strike in zip(contract_ids, strikes)
    )
    rows = tuple(
        _fixture_provider_row(
            provider_record_id=f"{source_document_id}.decision.{index}",
            contract_id=contract.contract_id,
            observed_at=_utc_text(observed),
            knowledge_at=_utc_text(known),
            bid=bid,
            ask=ask,
            source_hash=source_hash,
            schema_hash=schema_hash,
        )
        for index, (contract, (bid, ask)) in enumerate(
            zip(contracts, quotes),
            start=1,
        )
    )
    valuations = tuple(
        _fixture_valuation(
            valuation_input_id=f"{source_document_id}.decision.{contract.contract_id}",
            contract=contract,
            row=row,
            valuation_at=_utc_text(valuation),
            spot_price=spot_price,
            source_manifest_hashes=admitted_sources,
        )
        for contract, row in zip(contracts, rows)
    )
    repricing_state = _fixture_market_state(
        state_id=f"{source_document_id}.t03.repricing",
        valuation_at=_utc_text(resolved_repricing_valuation),
        observed_at=_utc_text(resolved_repricing_observed),
        knowledge_at=_utc_text(resolved_repricing_known),
        source_hash=source_hash,
        underlying_id=underlying_id,
        spot_price=resolved_repricing_spot,
        currency=resolved_currency,
    )
    repricing_rows = tuple(
        _fixture_provider_row(
            provider_record_id=f"{source_document_id}.repricing.{index}",
            contract_id=contract.contract_id,
            observed_at=_utc_text(resolved_repricing_observed),
            knowledge_at=_utc_text(resolved_repricing_known),
            bid=bid,
            ask=ask,
            source_hash=source_hash,
            schema_hash=schema_hash,
        )
        for index, (contract, (bid, ask)) in enumerate(
            zip(contracts, resolved_repricing_quotes),
            start=1,
        )
    )
    repricing_inputs = tuple(
        PublicOptionRepricingPrimitive(
            market_state=repricing_state,
            valuation_input=_fixture_valuation(
                valuation_input_id=(
                    f"{source_document_id}.repricing.{contract.contract_id}"
                ),
                contract=contract,
                row=row,
                valuation_at=_utc_text(resolved_repricing_valuation),
                spot_price=resolved_repricing_spot,
                source_manifest_hashes=admitted_sources,
            ),
            provider_row=row,
        )
        for contract, row in zip(contracts, repricing_rows)
    )
    settlement_availability = _fixture_availability(
        observed_at=_utc_text(resolved_settlement_observed),
        knowledge_at=_utc_text(resolved_settlement_known),
    )
    settlement_inputs = tuple(
        OptionSettlementInput(
            settlement_input_id=f"{source_document_id}.settlement.{contract.contract_id}",
            contract_id=contract.contract_id,
            settlement_at=_utc_text(resolved_settlement_observed),
            availability=settlement_availability,
            spot_price=resolved_settlement_spot,
            source_manifest_hash=source_hash,
        )
        for contract in contracts
    )
    return build_public_t03_inputs(
        profile_input_id=f"{source_document_id}.t03.input",
        provenance=(
            PublicOptionInputProvenance.EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE
        ),
        chain_id=f"{source_document_id}.t03.chain",
        underlying_id=underlying_id,
        market_state=state,
        contracts=contracts,
        valuation_inputs=valuations,
        provider_rows=rows,
        provider_conventions=(standard_provider_adapters()[0].convention,),
        source_manifest_hashes=admitted_sources,
        repricing_inputs=repricing_inputs,
        lifecycle=PublicOptionLifecyclePrimitive(
            kind=PublicOptionLifecycleKind.EXPIRY,
            event_at=_utc_text(resolved_lifecycle_event),
            settlement_inputs=settlement_inputs,
        ),
    )


__all__ = (
    "PUBLIC_OPTION_PROFILE_SCHEMA_VERSION",
    "PublicOptionInputProvenance",
    "PublicOptionInstitutionalFactory",
    "PublicOptionInstitutionalReceipt",
    "PublicOptionLifecycleKind",
    "PublicOptionLifecyclePrimitive",
    "PublicOptionProfileError",
    "PublicOptionProfileInput",
    "PublicOptionRepricingPrimitive",
    "PublicOptionSelectionParameters",
    "build_public_t03_fixture_inputs",
    "build_public_t03_inputs",
    "default_public_option_institutional_factory",
    "run_public_option_profile",
)
