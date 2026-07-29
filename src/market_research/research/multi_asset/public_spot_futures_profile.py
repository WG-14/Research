"""Source-owned public conformance profiles for spot and futures research.

The profiles in this module are deliberately separate from ``builtin_runner``.
They consume immutable, already-prepared domain inputs and execute the
institutional path themselves.  A caller may submit an expected instrument,
contract, or multiplier as an assertion, but those values never drive the
calculation.  Factory-only receipts bind every derived result to the raw source
hashes that produced it.

The deterministic builders at the bottom create *synthetic conformance
fixtures*.  They are useful for public T-01/T-02 capability proofs, but are
explicitly labelled and must never be represented as observed market history.
No network collection, probing, retry, or backfill is performed here.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from market_research.research.hashing import sha256_prefixed
from market_research.research.instrument_kinds import InstrumentKind
from market_research.research.market_calendar_contract import (
    MARKET_CALENDAR_SCHEMA_VERSION,
    MarketCalendarAuthority,
)
from market_research.research.multi_asset.costs import (
    CostMarketFeatures,
    EmpiricalCalibrationPolicy,
    EmpiricalCalibrationRegistry,
    EmpiricalExecutionCostModel,
    EmpiricalImpactCalibration,
    EnhancedCapacityInput,
    ExecutionContext,
    ExecutionSide,
    analyze_enhanced_capacity,
)
from market_research.research.multi_asset.domain import (
    ContractSpecification,
    EconomicUnderlying,
    EffectivePeriod,
    Instrument,
    InstrumentRelationship,
    InstrumentRelationshipType,
    InstrumentRegistry,
    Issuer,
    IssuerIdentifierNamespace,
    IssuerIdentifierRevision,
    Listing,
    SettlementType,
    SourceReference,
    SymbolAlias,
)
from market_research.research.multi_asset.exposure import (
    ExposureEngine,
    ExposurePosition,
    ExtendedPortfolioExposure,
    FuturesValuationAdapter,
    OffsetPolicyV3,
    PortfolioExposureSnapshot,
    PositionRiskSupplement,
    build_extended_portfolio_exposure,
)
from market_research.research.multi_asset.futures_delivery import (
    CollateralAssetKind,
    CollateralEligibility,
    CollateralHolding,
    ContinuousSeriesManifest,
    ContractSelectionPolicy,
    DeliverableBasket,
    DeliverableGrade,
    DeliveryPolicy,
    FuturesContractMasterHistory,
    FuturesContractMasterVersion,
    FuturesMarginPolicyVersion,
    FuturesSelectionCandidate,
    FuturesSettlementMode,
    FuturesTermsMetadata,
    RollAdjustmentMethod,
    evaluate_margin_waterfall,
    select_actual_contract,
    select_cheapest_to_deliver,
    settle_futures_position,
)
from market_research.research.multi_asset.market_state import (
    FuturesContractQuote,
    FuturesCurveState,
    LiquidityQuote,
    MarketState,
    ObservationMetadata,
    QuoteCondition,
    SpotQuote,
)
from market_research.research.multi_asset.normalization import (
    CalendarRegistry,
    CorporateActionAdjustment,
    EpochScaledReciprocalAdapter,
    IsoLocalDirectAdapter,
    MissingValueSemantics,
    NormalizationResult,
    ProviderNormalizationPolicy,
    ProviderNormalizationService,
    ProviderRow,
    QuoteConvention,
    UnitDefinition,
    UnitRegistry,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    PortfolioSnapshot,
    UnifiedPortfolioLedger,
    adapt_borrow_recall_application,
    adapt_corporate_action_application,
    adapt_futures_lifecycle_posting,
    adapt_futures_margin_waterfall,
    funding_event,
    trade_event,
)
from market_research.research.multi_asset.scenarios import (
    ConstrainedJointScenarioEngine,
    EconomicProjectionPolicy,
    JointMarketShock,
    ShockedMarketState,
)
from market_research.research.multi_asset.spot import (
    BorrowRecall,
    BorrowRecallReason,
    BorrowRecallRevisionStore,
    CashBalance,
    CorporateAction,
    CorporateActionRevisionStore,
    CorporateActionType,
    PointInTimeSpotUniverse,
    SpotBook,
    SpotPosition,
    UniverseMembership,
    apply_borrow_recall,
    apply_corporate_action,
)


PUBLIC_SPOT_FUTURES_PROFILE_SCHEMA_VERSION = 1
PUBLIC_T01_PROFILE_ID = "public.multi_asset.t01.spot.institutional.v1"
PUBLIC_T02_PROFILE_ID = "public.multi_asset.t02.futures.institutional.v1"
_ZERO = Decimal("0")
_ONE = Decimal("1")
_RECEIPT_FACTORY_TOKEN = object()
_HASH_LENGTH = len("sha256:") + 64


class PublicProfileError(ValueError):
    """An input can bypass or contradict a source-owned public profile."""


class InputFixtureSemantics(StrEnum):
    """Honest provenance label carried into every receipt."""

    EXTERNALLY_PREPARED_IMMUTABLE = "EXTERNALLY_PREPARED_IMMUTABLE"
    EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE = (
        "EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE"
    )


def _require_hash(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _HASH_LENGTH
        or not value.startswith("sha256:")
    ):
        raise PublicProfileError(f"{field_name}_invalid_hash")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise PublicProfileError(f"{field_name}_invalid_hash") from exc


def _require_hashes(values: tuple[str, ...], field_name: str) -> None:
    if not values or values != tuple(sorted(set(values))):
        raise PublicProfileError(f"{field_name}_not_canonical")
    for value in values:
        _require_hash(value, field_name)


def _instant(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PublicProfileError(f"{field_name}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicProfileError(f"{field_name}_timezone_required")
    return parsed.astimezone(UTC)


def _instant_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _sorted_hashes(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    for value in result:
        _require_hash(value, "source_hash")
    return result


def _balance_map(values: Sequence[object]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for value in values:
        currency = getattr(value, "currency")
        amount = getattr(value, "amount")
        if not isinstance(currency, str) or not isinstance(amount, Decimal):
            raise PublicProfileError("balance_protocol_invalid")
        result[currency] = amount
    return result


@dataclass(frozen=True, slots=True)
class ProviderConventionSource:
    """One immutable provider row and its reviewed normalization contract."""

    row: ProviderRow
    policy: ProviderNormalizationPolicy
    adapter_id: str

    def source_hash(self) -> str:
        return sha256_prefixed(
            {
                "row_hash": self.row.row_hash(),
                "policy_hash": self.policy.policy_hash(),
                "adapter_id": self.adapter_id,
            },
            label="public_provider_convention_source",
        )


@dataclass(frozen=True, slots=True)
class IssuerIdentifierQuery:
    namespace: IssuerIdentifierNamespace
    value: str
    jurisdiction: str | None = None
    provider_id: str | None = None


@dataclass(frozen=True, slots=True)
class EmpiricalCostCapacitySource:
    """Reviewed empirical calibration plus primitive market features."""

    calibrations: tuple[EmpiricalImpactCalibration, ...]
    policy: EmpiricalCalibrationPolicy
    observed_at: str
    regime_id: str
    quantity_grid: tuple[Decimal, ...]
    daily_capacity_quantity: Decimal
    daily_volatility: Decimal
    half_spread_bps: Decimal
    margin_requirement: Decimal
    margin_funding_bps: Decimal
    gross_edge_bps: Decimal
    target_degradation_fraction: Decimal
    source_hashes: tuple[str, ...]
    order_mode: str = "SIMULTANEOUS"
    leg_count: int = 1
    leg_interaction_fraction: Decimal = _ZERO

    def __post_init__(self) -> None:
        _instant(self.observed_at, "empirical_source.observed_at")
        if (
            not self.quantity_grid
            or self.quantity_grid != tuple(sorted(set(self.quantity_grid)))
            or any(value <= _ZERO for value in self.quantity_grid)
        ):
            raise PublicProfileError("empirical_source_quantity_grid_invalid")
        if self.daily_capacity_quantity <= _ZERO:
            raise PublicProfileError("empirical_source_daily_capacity_invalid")
        if self.quantity_grid[-1] > self.daily_capacity_quantity:
            raise PublicProfileError("empirical_source_quantity_above_capacity")
        for value, name in (
            (self.daily_volatility, "daily_volatility"),
            (self.half_spread_bps, "half_spread_bps"),
            (self.margin_requirement, "margin_requirement"),
            (self.margin_funding_bps, "margin_funding_bps"),
            (self.gross_edge_bps, "gross_edge_bps"),
            (self.target_degradation_fraction, "target_degradation_fraction"),
            (self.leg_interaction_fraction, "leg_interaction_fraction"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < _ZERO:
                raise PublicProfileError(f"empirical_source_{name}_invalid")
        if (
            self.target_degradation_fraction > _ONE
            or self.leg_interaction_fraction > _ONE
        ):
            raise PublicProfileError("empirical_source_fraction_above_one")
        _require_hashes(self.source_hashes, "empirical_source_hashes")


@dataclass(frozen=True, slots=True)
class PublicSpotProfileInputs:
    fixture_semantics: InputFixtureSemantics
    source_document_id: str
    source_document_hashes: tuple[str, ...]
    normalization_service: ProviderNormalizationService
    provider_conventions: tuple[ProviderConventionSource, ...]
    product_registry: InstrumentRegistry
    issuer_query: IssuerIdentifierQuery
    universe: PointInTimeSpotUniverse
    universe_id: str
    universe_source_hashes: tuple[str, ...]
    effective_at: str
    knowledge_at: str
    opened_at: str
    initial_book: SpotBook
    initial_book_source_hash: str
    corporate_actions: tuple[CorporateAction, ...]
    borrow_recalls: tuple[BorrowRecall, ...]
    borrow_recall_id: str
    recall_applied_at: str
    recall_execution_price: Decimal
    recall_execution_quote_hash: str
    recall_commission_per_unit: Decimal
    cost_source: EmpiricalCostCapacitySource
    market_state: MarketState
    economic_policy: EconomicProjectionPolicy
    stress_return: Decimal
    liquidity_haircut: Decimal
    liquidity_cost_multiplier: Decimal
    claimed_instrument_id: str | None = None
    claimed_issuer_id: str | None = None
    claimed_listing_id: str | None = None


@dataclass(frozen=True, slots=True)
class FuturesMarginSource:
    policy: FuturesMarginPolicyVersion
    collateral_holdings: tuple[CollateralHolding, ...]
    outright_contracts: Decimal
    spread_contract_pairs: Decimal
    variation_margin: Decimal
    elapsed_days: Decimal
    occurred_at: str


@dataclass(frozen=True, slots=True)
class PublicFuturesProfileInputs:
    fixture_semantics: InputFixtureSemantics
    source_document_id: str
    source_document_hashes: tuple[str, ...]
    continuous_manifest: ContinuousSeriesManifest
    contract_history: FuturesContractMasterHistory
    quote_candidates: tuple[FuturesSelectionCandidate, ...]
    selection_policy: ContractSelectionPolicy
    root_id: str
    decision_at: str
    knowledge_at: str
    opened_at: str
    position_quantity: Decimal
    initial_cash: Decimal
    initial_funding_source_hash: str
    margin_source: FuturesMarginSource
    final_settlement_price: Decimal
    final_settlement_at: str
    final_settlement_source_hash: str
    delivery_policy: DeliveryPolicy
    deliverable_basket: DeliverableBasket | None
    product_registry: InstrumentRegistry
    market_state: MarketState
    economic_policy: EconomicProjectionPolicy
    stress_return: Decimal
    stress_basis_shift: Decimal
    liquidity_haircut: Decimal
    claimed_selected_contract_id: str | None = None
    claimed_contract_multiplier: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PublicSpotProfileReceipt:
    profile_id: str
    fixture_semantics: InputFixtureSemantics
    input_hash: str
    normalization_receipt_hashes: tuple[str, ...]
    normalized_record_hashes: tuple[str, ...]
    resolved_instrument_id: str
    resolved_issuer_id: str
    resolved_listing_id: str
    universe_members: tuple[str, ...]
    corporate_action_hashes: tuple[str, ...]
    borrow_recall_hash: str
    ledger_hash: str
    ledger_head_hash: str
    ledger_event_hashes: tuple[str, ...]
    final_position_quantities: tuple[tuple[str, Decimal], ...]
    ledger_realized_pnl: tuple[tuple[str, Decimal], ...]
    ledger_income: tuple[tuple[str, Decimal], ...]
    ledger_costs: tuple[tuple[str, Decimal], ...]
    empirical_registry_hash: str
    empirical_estimate_hashes: tuple[str, ...]
    capacity_study_hash: str
    exposure_hash: str
    extended_exposure_hash: str
    scenario_hash: str
    economic_evidence_hash: str
    attribution_hash: str
    source_hashes: tuple[str, ...]
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)
    schema_version: int = PUBLIC_SPOT_FUTURES_PROFILE_SCHEMA_VERSION

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _RECEIPT_FACTORY_TOKEN:
            raise PublicProfileError("spot_receipt_factory_only")
        for name in (
            "input_hash",
            "borrow_recall_hash",
            "ledger_hash",
            "ledger_head_hash",
            "empirical_registry_hash",
            "capacity_study_hash",
            "exposure_hash",
            "extended_exposure_hash",
            "scenario_hash",
            "economic_evidence_hash",
            "attribution_hash",
        ):
            _require_hash(getattr(self, name), f"spot_receipt.{name}")
        for name in (
            "normalization_receipt_hashes",
            "normalized_record_hashes",
            "corporate_action_hashes",
            "ledger_event_hashes",
            "empirical_estimate_hashes",
            "source_hashes",
        ):
            _require_hashes(getattr(self, name), f"spot_receipt.{name}")
        for name in (
            "final_position_quantities",
            "ledger_realized_pnl",
            "ledger_income",
            "ledger_costs",
        ):
            rows = getattr(self, name)
            if rows != tuple(sorted(rows)) or len({key for key, _ in rows}) != len(
                rows
            ):
                raise PublicProfileError(f"spot_receipt.{name}_not_canonical")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="public_t01_spot_receipt"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "fixture_semantics": self.fixture_semantics.value,
            "input_hash": self.input_hash,
            "normalization_receipt_hashes": list(self.normalization_receipt_hashes),
            "normalized_record_hashes": list(self.normalized_record_hashes),
            "resolved_instrument_id": self.resolved_instrument_id,
            "resolved_issuer_id": self.resolved_issuer_id,
            "resolved_listing_id": self.resolved_listing_id,
            "universe_members": list(self.universe_members),
            "corporate_action_hashes": list(self.corporate_action_hashes),
            "borrow_recall_hash": self.borrow_recall_hash,
            "ledger_hash": self.ledger_hash,
            "ledger_head_hash": self.ledger_head_hash,
            "ledger_event_hashes": list(self.ledger_event_hashes),
            "final_position_quantities": [
                {"instrument_id": key, "quantity": _decimal_text(value)}
                for key, value in self.final_position_quantities
            ],
            "ledger_realized_pnl": [
                {"currency": key, "amount": _decimal_text(value)}
                for key, value in self.ledger_realized_pnl
            ],
            "ledger_income": [
                {"currency": key, "amount": _decimal_text(value)}
                for key, value in self.ledger_income
            ],
            "ledger_costs": [
                {"currency": key, "amount": _decimal_text(value)}
                for key, value in self.ledger_costs
            ],
            "empirical_registry_hash": self.empirical_registry_hash,
            "empirical_estimate_hashes": list(self.empirical_estimate_hashes),
            "capacity_study_hash": self.capacity_study_hash,
            "exposure_hash": self.exposure_hash,
            "extended_exposure_hash": self.extended_exposure_hash,
            "scenario_hash": self.scenario_hash,
            "economic_evidence_hash": self.economic_evidence_hash,
            "attribution_hash": self.attribution_hash,
            "source_hashes": list(self.source_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class PublicFuturesProfileReceipt:
    profile_id: str
    fixture_semantics: InputFixtureSemantics
    input_hash: str
    continuous_manifest_hash: str
    contract_history_hash: str
    selected_contract_id: str
    selected_contract_hash: str
    selected_contract_multiplier: Decimal
    position_quantity: Decimal
    prior_settlement_price: Decimal
    final_settlement_price: Decimal
    selection_receipt_hash: str
    resolved_contract_version: int
    margin_policy_hash: str
    margin_waterfall_hash: str
    variation_margin: Decimal
    lifecycle_cash_delta: Decimal
    ctd_decision_hash: str | None
    lifecycle_posting_hashes: tuple[str, ...]
    pre_settlement_ledger_hash: str
    final_ledger_hash: str
    final_ledger_head_hash: str
    final_ledger_event_hashes: tuple[str, ...]
    final_realized_pnl: tuple[tuple[str, Decimal], ...]
    exposure_hash: str
    extended_exposure_hash: str
    scenario_hash: str
    economic_evidence_hash: str
    provenance_hash: str
    source_hashes: tuple[str, ...]
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)
    schema_version: int = PUBLIC_SPOT_FUTURES_PROFILE_SCHEMA_VERSION

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _RECEIPT_FACTORY_TOKEN:
            raise PublicProfileError("futures_receipt_factory_only")
        for name in (
            "input_hash",
            "continuous_manifest_hash",
            "contract_history_hash",
            "selected_contract_hash",
            "selection_receipt_hash",
            "margin_policy_hash",
            "margin_waterfall_hash",
            "pre_settlement_ledger_hash",
            "final_ledger_hash",
            "final_ledger_head_hash",
            "exposure_hash",
            "extended_exposure_hash",
            "scenario_hash",
            "economic_evidence_hash",
            "provenance_hash",
        ):
            _require_hash(getattr(self, name), f"futures_receipt.{name}")
        if self.ctd_decision_hash is not None:
            _require_hash(self.ctd_decision_hash, "futures_receipt.ctd_decision_hash")
        for name in (
            "lifecycle_posting_hashes",
            "final_ledger_event_hashes",
            "source_hashes",
        ):
            _require_hashes(getattr(self, name), f"futures_receipt.{name}")
        if self.resolved_contract_version < 1:
            raise PublicProfileError("futures_receipt_contract_version_invalid")
        for name in (
            "selected_contract_multiplier",
            "prior_settlement_price",
            "final_settlement_price",
        ):
            value = getattr(self, name)
            if value <= _ZERO:
                raise PublicProfileError(f"futures_receipt_{name}_invalid")
        if self.position_quantity == _ZERO:
            raise PublicProfileError("futures_receipt_position_quantity_invalid")
        if self.final_realized_pnl != tuple(sorted(self.final_realized_pnl)):
            raise PublicProfileError("futures_receipt_realized_pnl_not_canonical")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="public_t02_futures_receipt"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "fixture_semantics": self.fixture_semantics.value,
            "input_hash": self.input_hash,
            "continuous_manifest_hash": self.continuous_manifest_hash,
            "contract_history_hash": self.contract_history_hash,
            "selected_contract_id": self.selected_contract_id,
            "selected_contract_hash": self.selected_contract_hash,
            "selected_contract_multiplier": _decimal_text(
                self.selected_contract_multiplier
            ),
            "position_quantity": _decimal_text(self.position_quantity),
            "prior_settlement_price": _decimal_text(self.prior_settlement_price),
            "final_settlement_price": _decimal_text(self.final_settlement_price),
            "selection_receipt_hash": self.selection_receipt_hash,
            "resolved_contract_version": self.resolved_contract_version,
            "margin_policy_hash": self.margin_policy_hash,
            "margin_waterfall_hash": self.margin_waterfall_hash,
            "variation_margin": _decimal_text(self.variation_margin),
            "lifecycle_cash_delta": _decimal_text(self.lifecycle_cash_delta),
            "ctd_decision_hash": self.ctd_decision_hash,
            "lifecycle_posting_hashes": list(self.lifecycle_posting_hashes),
            "pre_settlement_ledger_hash": self.pre_settlement_ledger_hash,
            "final_ledger_hash": self.final_ledger_hash,
            "final_ledger_head_hash": self.final_ledger_head_hash,
            "final_ledger_event_hashes": list(self.final_ledger_event_hashes),
            "final_realized_pnl": [
                {"currency": key, "amount": _decimal_text(value)}
                for key, value in self.final_realized_pnl
            ],
            "exposure_hash": self.exposure_hash,
            "extended_exposure_hash": self.extended_exposure_hash,
            "scenario_hash": self.scenario_hash,
            "economic_evidence_hash": self.economic_evidence_hash,
            "provenance_hash": self.provenance_hash,
            "source_hashes": list(self.source_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class _FuturesStateRepricer:
    contract_id: str

    def reprice(
        self,
        position: object,
        *,
        market_state: object,
        shocked_state: ShockedMarketState,
    ) -> Decimal:
        del position, market_state
        return shocked_state.price_for(self.contract_id)


def _seed_spot_ledger(
    inputs: PublicSpotProfileInputs,
    *,
    source_hashes: tuple[str, ...],
) -> UnifiedPortfolioLedger:
    opened = _instant(inputs.opened_at, "spot.opened_at")
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="public.t01.spot.ledger",
        base_currency=inputs.market_state.base_currency,
    )
    trade_drafts = []
    trade_cash: dict[str, Decimal] = {}
    for index, position in enumerate(inputs.initial_book.positions, start=1):
        if position.quantity == _ZERO or position.total_cost_basis <= _ZERO:
            raise PublicProfileError("spot_initial_position_invalid")
        price = position.total_cost_basis / abs(position.quantity)
        trade_drafts.append(
            trade_event(
                event_id=f"public.t01.initial.trade.{index}",
                occurred_at=_instant_text(opened + timedelta(seconds=index)),
                asset_class=AssetClass.SPOT,
                instrument_id=position.instrument_id,
                currency=position.currency,
                quantity_delta=position.quantity,
                price=price,
                source_hashes=source_hashes,
            )
        )
        trade_cash[position.currency] = (
            trade_cash.get(position.currency, _ZERO) - position.quantity * price
        )
    target_cash = _balance_map(inputs.initial_book.cash)
    currencies = set(target_cash) | set(trade_cash)
    funding_deltas = tuple(
        CashDelta(
            currency, target_cash.get(currency, _ZERO) - trade_cash.get(currency, _ZERO)
        )
        for currency in sorted(currencies)
        if target_cash.get(currency, _ZERO) - trade_cash.get(currency, _ZERO) != _ZERO
    )
    if not funding_deltas:
        raise PublicProfileError("spot_initial_funding_required")
    ledger = ledger.publish(
        funding_event(
            event_id="public.t01.initial.funding",
            occurred_at=_instant_text(opened),
            cash_deltas=funding_deltas,
            source_hashes=source_hashes,
        )
    )
    return ledger.publish_many(tuple(trade_drafts))


def _verify_spot_book_projection(
    book: SpotBook,
    snapshot: PortfolioSnapshot,
) -> None:
    expected_positions = {
        item.instrument_id: (
            item.quantity,
            item.total_cost_basis,
            item.currency,
        )
        for item in book.positions
    }
    actual_positions = {
        item.instrument_id: (
            item.quantity,
            abs(item.quantity) * item.average_price * item.multiplier,
            item.currency,
        )
        for item in snapshot.positions
        if item.asset_class is AssetClass.SPOT
    }
    if expected_positions != actual_positions:
        raise PublicProfileError("spot_ledger_book_position_mismatch")
    if _balance_map(book.cash) != _balance_map(snapshot.cash):
        raise PublicProfileError("spot_ledger_book_cash_mismatch")


def _exposure_positions(
    snapshot: PortfolioSnapshot,
    *,
    registry: InstrumentRegistry,
    market_state: MarketState,
    opened_at: str,
) -> tuple[ExposurePosition, ...]:
    result = []
    for index, position in enumerate(snapshot.positions, start=1):
        instrument = registry.instrument_as_of(
            position.instrument_id,
            market_state.valuation_at,
            knowledge_at=market_state.valuation_at,
        )
        if instrument is None:
            raise PublicProfileError(
                f"exposure_instrument_not_authoritative:{position.instrument_id}"
            )
        result.append(
            ExposurePosition(
                position_id=f"public.position.{index}.{position.instrument_id}",
                instrument_id=position.instrument_id,
                quantity=position.quantity,
                quantity_unit=instrument.unit,
                multiplier=position.multiplier,
                currency=position.currency,
                source_hash=snapshot.content_hash,
                opened_at=opened_at,
            )
        )
    if not result:
        raise PublicProfileError("profile_open_positions_required")
    return tuple(result)


def _extended_exposure(
    exposure: PortfolioExposureSnapshot,
    *,
    source_hashes: tuple[str, ...],
) -> ExtendedPortfolioExposure:
    positions = exposure.positions
    supplements = tuple(
        PositionRiskSupplement(
            position_id=position.position_id,
            position_hash=position.position_hash,
            valuation_hash=position.valuation_hash,
            beta_equivalent=position.delta_base,
            duration=_ZERO,
            dv01=_ZERO,
            fx_exposure=position.net_notional_base,
            commodity_units=position.quantity * position.multiplier,
            vanna=_ZERO,
            volga=_ZERO,
            charm=_ZERO,
            factor_ids=(f"factor.{position.underlying_id}",),
            funding_bucket=f"funding.{position.native_currency.lower()}",
            tenor_bucket=position.expiry_at or "NON_EXPIRING",
            volatility_bucket="NON_OPTION",
            source_hashes=source_hashes,
        )
        for position in positions
    )
    return build_extended_portfolio_exposure(
        exposure,
        supplements=supplements,
        offset_policy=OffsetPolicyV3(
            policy_id="public.cross_asset.offsets",
            version="1",
        ),
    )


def _spot_input_hash(inputs: PublicSpotProfileInputs) -> str:
    return sha256_prefixed(
        {
            "fixture_semantics": inputs.fixture_semantics.value,
            "source_document_id": inputs.source_document_id,
            "source_document_hashes": list(inputs.source_document_hashes),
            "provider_sources": [
                item.source_hash() for item in inputs.provider_conventions
            ],
            "product_registry_hash": inputs.product_registry.contract_hash(),
            "universe_source_hashes": list(inputs.universe_source_hashes),
            "initial_book_source_hash": inputs.initial_book_source_hash,
            "corporate_action_hashes": [
                item.content_hash for item in inputs.corporate_actions
            ],
            "borrow_recall_hashes": [
                item.content_hash for item in inputs.borrow_recalls
            ],
            "market_state_hash": inputs.market_state.state_hash(),
            "economic_policy_hash": inputs.economic_policy.content_hash,
        },
        label="public_t01_spot_inputs",
    )


def _futures_input_hash(inputs: PublicFuturesProfileInputs) -> str:
    return sha256_prefixed(
        {
            "fixture_semantics": inputs.fixture_semantics.value,
            "source_document_id": inputs.source_document_id,
            "source_document_hashes": list(inputs.source_document_hashes),
            "continuous_manifest_hash": inputs.continuous_manifest.manifest_hash(),
            "contract_history_hash": inputs.contract_history.content_hash(),
            "candidate_hashes": [
                item.candidate_hash() for item in inputs.quote_candidates
            ],
            "selection_policy_hash": inputs.selection_policy.policy_hash(),
            "margin_policy_hash": inputs.margin_source.policy.policy_hash(),
            "delivery_policy_hash": inputs.delivery_policy.policy_hash(),
            "basket_hash": (
                inputs.deliverable_basket.basket_hash()
                if inputs.deliverable_basket is not None
                else None
            ),
            "product_registry_hash": inputs.product_registry.contract_hash(),
            "market_state_hash": inputs.market_state.state_hash(),
            "economic_policy_hash": inputs.economic_policy.content_hash,
            "final_settlement_source_hash": inputs.final_settlement_source_hash,
        },
        label="public_t02_futures_inputs",
    )


@dataclass(frozen=True, slots=True)
class PublicSpotFuturesConformanceService:
    """Only authority allowed to mint public T-01/T-02 receipts."""

    service_id: str = "public.spot_futures.conformance"
    service_version: str = "1"

    def run_spot(self, inputs: PublicSpotProfileInputs) -> PublicSpotProfileReceipt:
        if not isinstance(inputs.fixture_semantics, InputFixtureSemantics):
            raise PublicProfileError("spot_fixture_semantics_required")
        _require_hashes(inputs.source_document_hashes, "source_document_hashes")
        _require_hashes(inputs.universe_source_hashes, "universe_source_hashes")
        _require_hash(inputs.initial_book_source_hash, "initial_book_source_hash")
        _require_hash(inputs.recall_execution_quote_hash, "recall_execution_quote_hash")
        effective = _instant(inputs.effective_at, "spot.effective_at")
        knowledge = _instant(inputs.knowledge_at, "spot.knowledge_at")
        if knowledge < effective:
            raise PublicProfileError("spot_knowledge_before_effective")
        if len(inputs.provider_conventions) != 2:
            raise PublicProfileError("spot_two_provider_conventions_required")
        if len({item.row.provider_id for item in inputs.provider_conventions}) != 2:
            raise PublicProfileError("spot_provider_conventions_not_distinct")
        normalized: list[NormalizationResult] = []
        store = None
        for convention in inputs.provider_conventions:
            result = inputs.normalization_service.normalize(
                convention.row,
                policy=convention.policy,
                adapter_id=convention.adapter_id,
                store=store,
            )
            store = result.store
            normalized.append(result)
        economic_fields = (
            "event_at",
            "trading_date",
            "session_id",
            "instrument_id",
            "price",
            "quantity",
            "currency",
            "price_unit",
            "quantity_unit",
            "contract_multiplier",
        )
        if any(
            normalized[0].normalized_record.payload[field_name]
            != normalized[1].normalized_record.payload[field_name]
            for field_name in economic_fields
        ):
            raise PublicProfileError("spot_provider_economic_meaning_mismatch")
        normalized_reference_price = Decimal(
            str(normalized[0].normalized_record.payload["price"])
        )
        resolved_ids: set[str] = set()
        for convention in inputs.provider_conventions:
            resolved_ids.add(
                inputs.product_registry.resolve_symbol(
                    provider_id=convention.policy.provider_id,
                    symbol=convention.policy.provider_symbol,
                    as_of=inputs.effective_at,
                    knowledge_at=inputs.knowledge_at,
                ).instrument_id
            )
            if convention.policy.instrument_id not in resolved_ids:
                raise PublicProfileError("spot_normalization_product_binding_mismatch")
        if len(resolved_ids) != 1:
            raise PublicProfileError("spot_provider_symbol_resolution_mismatch")
        resolved_id = next(iter(resolved_ids))
        instrument = inputs.product_registry.tradable_instrument_as_of(
            resolved_id,
            inputs.effective_at,
            knowledge_at=inputs.knowledge_at,
        )
        if instrument is None or instrument.issuer_id is None:
            raise PublicProfileError("spot_instrument_not_tradable_or_issued")
        issuer = inputs.product_registry.resolve_issuer_identifier(
            namespace=inputs.issuer_query.namespace,
            value=inputs.issuer_query.value,
            as_of=inputs.effective_at,
            knowledge_at=inputs.knowledge_at,
            jurisdiction=inputs.issuer_query.jurisdiction,
            provider_id=inputs.issuer_query.provider_id,
        )
        if issuer.issuer_id != instrument.issuer_id:
            raise PublicProfileError("spot_issuer_identifier_binding_mismatch")
        if instrument.primary_listing_id is None:
            raise PublicProfileError("spot_primary_listing_required")
        listing = inputs.product_registry.listing_as_of(
            instrument.primary_listing_id,
            inputs.effective_at,
            knowledge_at=inputs.knowledge_at,
        )
        if listing is None or listing.instrument_id != resolved_id:
            raise PublicProfileError("spot_listing_binding_mismatch")
        members = inputs.universe.members(
            inputs.universe_id,
            effective_at=effective,
            knowledge_at=knowledge,
        )
        if resolved_id not in members:
            raise PublicProfileError("spot_resolved_instrument_outside_universe")
        claims = (
            (inputs.claimed_instrument_id, resolved_id, "instrument"),
            (inputs.claimed_issuer_id, issuer.issuer_id, "issuer"),
            (inputs.claimed_listing_id, listing.listing_id, "listing"),
        )
        for claimed, actual, label in claims:
            if claimed is not None and claimed != actual:
                raise PublicProfileError(f"spot_forged_{label}_preselection")

        normalized_hashes = tuple(
            item.normalized_record.record_hash() for item in normalized
        )
        seed_sources = _sorted_hashes(
            (
                *inputs.source_document_hashes,
                inputs.initial_book_source_hash,
                inputs.product_registry.contract_hash(),
                *normalized_hashes,
            )
        )
        ledger = _seed_spot_ledger(inputs, source_hashes=seed_sources)
        book = inputs.initial_book
        required_action_types = {
            CorporateActionType.RIGHTS_ISSUE,
            CorporateActionType.RIGHTS_SUBSCRIPTION,
            CorporateActionType.MERGER,
        }
        actions = CorporateActionRevisionStore(inputs.corporate_actions).as_of(
            knowledge
        )
        if not required_action_types.issubset({item.action_type for item in actions}):
            raise PublicProfileError("spot_required_lifecycle_actions_missing")
        applications = []
        for action in sorted(
            actions, key=lambda item: (item.effective_at, item.action_id)
        ):
            entitlement_book = (
                book if action.action_type is CorporateActionType.RIGHTS_ISSUE else None
            )
            application = apply_corporate_action(
                book,
                action,
                applied_at=action.effective_at,
                entitlement_book=entitlement_book,
            )
            observed_marks = {
                item.instrument_id: item.price for item in inputs.market_state.spots
            }
            post_action_marks = {
                item.instrument_id: observed_marks.get(
                    item.instrument_id,
                    (
                        item.total_cost_basis / abs(item.quantity)
                        if item.total_cost_basis > _ZERO
                        else normalized_reference_price
                    ),
                )
                for item in application.book_after.positions
            }
            ledger = ledger.publish_many(
                adapt_corporate_action_application(
                    application,
                    mark_prices_after=post_action_marks,
                )
            )
            applications.append(application)
            book = application.book_after
        recalls = BorrowRecallRevisionStore(inputs.borrow_recalls).as_of(knowledge)
        selected_recalls = [
            item for item in recalls if item.recall_id == inputs.borrow_recall_id
        ]
        if len(selected_recalls) != 1:
            raise PublicProfileError("spot_borrow_recall_not_unique_as_of")
        recall = selected_recalls[0]
        recall_application = apply_borrow_recall(
            book,
            recall,
            applied_at=_instant(inputs.recall_applied_at, "recall.applied_at"),
            execution_price=inputs.recall_execution_price,
            execution_quote_hash=inputs.recall_execution_quote_hash,
            commission_per_unit=inputs.recall_commission_per_unit,
        )
        ledger = ledger.publish_many(
            adapt_borrow_recall_application(recall_application)
        )
        book = recall_application.book_after
        final_snapshot = ledger.replay()
        _verify_spot_book_projection(book, final_snapshot)

        normalized_payload = normalized[0].normalized_record.payload
        reference_price = Decimal(str(normalized_payload["price"]))
        cost_registry = EmpiricalCalibrationRegistry(
            calibrations=inputs.cost_source.calibrations,
            policy=inputs.cost_source.policy,
        )
        cost_model = EmpiricalExecutionCostModel(registry=cost_registry)
        cost_sources = _sorted_hashes(
            (
                *inputs.cost_source.source_hashes,
                *normalized_hashes,
                ledger.head_hash,
            )
        )
        capacity_inputs = []
        estimates = []
        for index, quantity in enumerate(inputs.cost_source.quantity_grid, start=1):
            participation = quantity / inputs.cost_source.daily_capacity_quantity
            context = ExecutionContext(
                execution_id=f"public.t01.capacity.{index}",
                instrument_id=resolved_id,
                instrument_kind="SPOT",
                currency=listing.trading_currency,
                side=ExecutionSide.BUY,
                requested_quantity=quantity,
                filled_quantity=quantity,
                reference_price=reference_price,
                execution_price=reference_price,
                observed_at=inputs.cost_source.observed_at,
                capacity_quantity=inputs.cost_source.daily_capacity_quantity,
                participation_rate=participation,
                source_hashes=cost_sources,
            )
            features = CostMarketFeatures(
                observed_at=inputs.cost_source.observed_at,
                regime_id=inputs.cost_source.regime_id,
                participation_rate=participation,
                daily_volatility=inputs.cost_source.daily_volatility,
                half_spread_bps=inputs.cost_source.half_spread_bps,
                order_mode=inputs.cost_source.order_mode,
                leg_count=inputs.cost_source.leg_count,
                leg_interaction_fraction=(inputs.cost_source.leg_interaction_fraction),
                margin_requirement=inputs.cost_source.margin_requirement,
                target_degradation_fraction=(
                    inputs.cost_source.target_degradation_fraction
                ),
                source_hashes=cost_sources,
            )
            estimates.append(cost_model.estimate(context, features))
            capacity_inputs.append(
                EnhancedCapacityInput(
                    context=context,
                    features=features,
                    daily_capacity_quantity=(
                        inputs.cost_source.daily_capacity_quantity
                    ),
                    margin_funding_bps=inputs.cost_source.margin_funding_bps,
                )
            )
        capacity = analyze_enhanced_capacity(
            tuple(capacity_inputs),
            model=cost_model,
            gross_edge_bps=inputs.cost_source.gross_edge_bps,
        )

        exposure_positions = _exposure_positions(
            final_snapshot,
            registry=inputs.product_registry,
            market_state=inputs.market_state,
            opened_at=inputs.opened_at,
        )
        exposure = ExposureEngine.with_default_spot(
            product_catalog=inputs.product_registry,
        ).evaluate(
            snapshot_id="public.t01.exposure",
            positions=exposure_positions,
            market_state=inputs.market_state,
        )
        extended = _extended_exposure(
            exposure,
            source_hashes=_sorted_hashes(
                (exposure.content_hash, inputs.market_state.state_hash())
            ),
        )
        if not -_ONE < inputs.stress_return:
            raise PublicProfileError("spot_stress_return_invalid")
        if not _ZERO <= inputs.liquidity_haircut <= _ONE:
            raise PublicProfileError("spot_liquidity_haircut_invalid")
        held_ids = tuple(
            sorted(item.instrument_id for item in final_snapshot.positions)
        )
        shock = JointMarketShock(
            scenario_id="public.t01.constrained.stress",
            price_returns=tuple(
                (instrument_id, inputs.stress_return) for instrument_id in held_ids
            ),
            liquidity_haircuts=tuple(
                (instrument_id, inputs.liquidity_haircut) for instrument_id in held_ids
            ),
            liquidity_cost_multiplier=inputs.liquidity_cost_multiplier,
            source_hashes=inputs.source_document_hashes,
        )
        liquidation_cost = sum(
            (item.implementation_shortfall for item in estimates), start=_ZERO
        ) / Decimal(len(estimates))
        scenario = ConstrainedJointScenarioEngine(
            policy=inputs.economic_policy
        ).evaluate(
            final_snapshot,
            market_state=inputs.market_state,
            shock=shock,
            base_liquidation_costs={
                instrument_id: liquidation_cost for instrument_id in held_ids
            },
        )
        attribution_hash = sha256_prefixed(
            {
                "portfolio_snapshot_hash": final_snapshot.content_hash,
                "realized_pnl": [
                    {"currency": item.currency, "amount": _decimal_text(item.amount)}
                    for item in final_snapshot.realized_pnl
                ],
                "income": [
                    {"currency": item.currency, "amount": _decimal_text(item.amount)}
                    for item in final_snapshot.income
                ],
                "costs": [
                    {"currency": item.currency, "amount": _decimal_text(item.amount)}
                    for item in final_snapshot.costs
                ],
                "event_source_hashes": [
                    list(item.source_hashes) for item in ledger.events
                ],
            },
            label="public_t01_spot_attribution",
        )
        source_hashes = _sorted_hashes(
            (
                *inputs.source_document_hashes,
                *inputs.universe_source_hashes,
                *normalized_hashes,
                inputs.product_registry.contract_hash(),
                inputs.market_state.state_hash(),
                cost_registry.content_hash,
                capacity.content_hash,
                exposure.content_hash,
                extended.content_hash,
                scenario.content_hash,
                attribution_hash,
            )
        )
        return PublicSpotProfileReceipt(
            profile_id=PUBLIC_T01_PROFILE_ID,
            fixture_semantics=inputs.fixture_semantics,
            input_hash=_spot_input_hash(inputs),
            normalization_receipt_hashes=tuple(
                sorted(item.receipt.receipt_hash() for item in normalized)
            ),
            normalized_record_hashes=tuple(sorted(normalized_hashes)),
            resolved_instrument_id=resolved_id,
            resolved_issuer_id=issuer.issuer_id,
            resolved_listing_id=listing.listing_id,
            universe_members=members,
            corporate_action_hashes=tuple(
                sorted(item.action_hash for item in applications)
            ),
            borrow_recall_hash=recall.content_hash,
            ledger_hash=ledger.content_hash,
            ledger_head_hash=ledger.head_hash,
            ledger_event_hashes=tuple(
                sorted(item.content_hash for item in ledger.events)
            ),
            final_position_quantities=tuple(
                sorted(
                    (item.instrument_id, item.quantity)
                    for item in final_snapshot.positions
                )
            ),
            ledger_realized_pnl=tuple(
                (item.currency, item.amount) for item in final_snapshot.realized_pnl
            ),
            ledger_income=tuple(
                (item.currency, item.amount) for item in final_snapshot.income
            ),
            ledger_costs=tuple(
                (item.currency, item.amount) for item in final_snapshot.costs
            ),
            empirical_registry_hash=cost_registry.content_hash,
            empirical_estimate_hashes=tuple(
                sorted(item.content_hash for item in estimates)
            ),
            capacity_study_hash=capacity.content_hash,
            exposure_hash=exposure.content_hash,
            extended_exposure_hash=extended.content_hash,
            scenario_hash=scenario.content_hash,
            economic_evidence_hash=scenario.economic_evidence.content_hash,
            attribution_hash=attribution_hash,
            source_hashes=source_hashes,
            _factory_token=_RECEIPT_FACTORY_TOKEN,
        )

    def run_futures(
        self, inputs: PublicFuturesProfileInputs
    ) -> PublicFuturesProfileReceipt:
        if not isinstance(inputs.fixture_semantics, InputFixtureSemantics):
            raise PublicProfileError("futures_fixture_semantics_required")
        _require_hashes(inputs.source_document_hashes, "source_document_hashes")
        _require_hash(inputs.initial_funding_source_hash, "initial_funding_source_hash")
        _require_hash(
            inputs.final_settlement_source_hash, "final_settlement_source_hash"
        )
        decision = _instant(inputs.decision_at, "futures.decision_at")
        knowledge = _instant(inputs.knowledge_at, "futures.knowledge_at")
        if knowledge > decision:
            raise PublicProfileError("futures_future_knowledge")
        if not inputs.quote_candidates:
            raise PublicProfileError("futures_quote_candidates_required")
        authoritative_hashes = {
            item.contract_hash() for item in inputs.contract_history.versions
        }
        candidates = []
        for candidate in inputs.quote_candidates:
            if candidate.contract.contract_hash() not in authoritative_hashes:
                raise PublicProfileError("futures_candidate_contract_not_authoritative")
            inputs.continuous_manifest.require_actual_contract(
                candidate.contract.contract_id
            )
            resolved = inputs.contract_history.resolve(
                candidate.contract.contract_id,
                valid_at=inputs.decision_at,
                knowledge_at=inputs.knowledge_at,
            )
            candidates.append(replace(candidate, contract=resolved))
        selection = select_actual_contract(
            tuple(candidates),
            root_id=inputs.root_id,
            decision_at=inputs.decision_at,
            knowledge_at=inputs.knowledge_at,
            policy=inputs.selection_policy,
        )
        selected_id = inputs.continuous_manifest.require_actual_contract(
            selection.selected_contract_id
        )
        selected = inputs.contract_history.resolve(
            selected_id,
            valid_at=inputs.decision_at,
            knowledge_at=inputs.knowledge_at,
        )
        if selection.selected_contract_hash != selected.contract_hash():
            raise PublicProfileError("futures_selection_master_binding_mismatch")
        if (
            inputs.claimed_selected_contract_id is not None
            and inputs.claimed_selected_contract_id != selected_id
        ):
            raise PublicProfileError("futures_forged_contract_preselection")
        if (
            inputs.claimed_contract_multiplier is not None
            and inputs.claimed_contract_multiplier != selected.contract_multiplier
        ):
            raise PublicProfileError("futures_forged_contract_multiplier")
        selected_candidate = next(
            item for item in candidates if item.contract.contract_id == selected_id
        )
        market_quote = inputs.market_state.futures_contract_quote(selected_id)
        if market_quote.settlement != selected_candidate.settlement_price:
            raise PublicProfileError("futures_market_selection_quote_mismatch")
        specification = inputs.product_registry.contract_specification_as_of(
            selected_id,
            inputs.market_state.valuation_at,
            knowledge_at=inputs.market_state.valuation_at,
        )
        if (
            specification is None
            or specification.contract_multiplier != selected.contract_multiplier
            or specification.settlement_currency != selected.settlement_currency
        ):
            raise PublicProfileError("futures_catalog_contract_terms_mismatch")

        ledger = UnifiedPortfolioLedger.open(
            ledger_id="public.t02.futures.ledger",
            base_currency=inputs.market_state.base_currency,
        )
        ledger = ledger.publish(
            funding_event(
                event_id="public.t02.initial.funding",
                occurred_at=inputs.opened_at,
                cash_deltas=(
                    CashDelta(
                        selected.settlement_currency,
                        inputs.initial_cash,
                    ),
                ),
                source_hashes=(inputs.initial_funding_source_hash,),
            )
        )
        ledger = ledger.publish(
            trade_event(
                event_id="public.t02.open.actual.contract",
                occurred_at=_instant_text(
                    _instant(inputs.opened_at, "futures.opened_at")
                    + timedelta(seconds=1)
                ),
                asset_class=AssetClass.FUTURE,
                instrument_id=selected_id,
                currency=selected.settlement_currency,
                quantity_delta=inputs.position_quantity,
                price=selected_candidate.settlement_price,
                multiplier=selected.contract_multiplier,
                source_hashes=_sorted_hashes(
                    (
                        selection.receipt_hash(),
                        selected.contract_hash(),
                        selected_candidate.source_quote_hash,
                    )
                ),
            )
        )
        risk_snapshot = ledger.replay()
        margin = evaluate_margin_waterfall(
            inputs.margin_source.policy,
            outright_contracts=inputs.margin_source.outright_contracts,
            spread_contract_pairs=inputs.margin_source.spread_contract_pairs,
            variation_margin=inputs.margin_source.variation_margin,
            collateral_holdings=inputs.margin_source.collateral_holdings,
            elapsed_days=inputs.margin_source.elapsed_days,
        )
        ledger = ledger.publish_many(
            adapt_futures_margin_waterfall(
                margin,
                event_id_prefix="public.t02.margin",
                occurred_at=inputs.margin_source.occurred_at,
                currency=selected.settlement_currency,
                contract_id=selected_id,
            )
        )
        pre_settlement_ledger = ledger
        exposure_positions = _exposure_positions(
            risk_snapshot,
            registry=inputs.product_registry,
            market_state=inputs.market_state,
            opened_at=inputs.opened_at,
        )
        exposure = ExposureEngine.with_default_spot(
            product_catalog=inputs.product_registry,
            derivative_adapters=(
                FuturesValuationAdapter(
                    margin_model_hash=market_quote.margin_model_hash,
                ),
            ),
        ).evaluate(
            snapshot_id="public.t02.exposure",
            positions=exposure_positions,
            market_state=inputs.market_state,
        )
        extended = _extended_exposure(
            exposure,
            source_hashes=_sorted_hashes(
                (exposure.content_hash, selected.contract_hash())
            ),
        )
        underlying_id = market_quote.underlying_instrument_id
        shock = JointMarketShock(
            scenario_id="public.t02.constrained.stress",
            price_returns=((underlying_id, inputs.stress_return),),
            futures_basis_shifts=((selected_id, inputs.stress_basis_shift),),
            liquidity_haircuts=((selected_id, inputs.liquidity_haircut),),
            source_hashes=inputs.source_document_hashes,
        )
        scenario = ConstrainedJointScenarioEngine(
            policy=inputs.economic_policy
        ).evaluate(
            risk_snapshot,
            market_state=inputs.market_state,
            shock=shock,
            repricers={selected_id: _FuturesStateRepricer(selected_id)},
            base_liquidation_costs={
                selected_id: abs(inputs.position_quantity)
                * selected_candidate.spread
                * selected.contract_multiplier
            },
        )
        ctd = None
        if selected.settlement_mode is FuturesSettlementMode.PHYSICAL:
            if inputs.deliverable_basket is None:
                raise PublicProfileError("futures_physical_basket_required")
            ctd = select_cheapest_to_deliver(
                selected,
                inputs.deliverable_basket,
                futures_settlement_price=inputs.final_settlement_price,
            )
        elif inputs.deliverable_basket is not None:
            raise PublicProfileError("futures_cash_basket_forbidden")
        lifecycle = settle_futures_position(
            selected,
            quantity=inputs.position_quantity,
            prior_settlement_price=selected_candidate.settlement_price,
            final_settlement_price=inputs.final_settlement_price,
            occurred_at=inputs.final_settlement_at,
            policy=inputs.delivery_policy,
            ctd=ctd,
        )
        for posting in lifecycle:
            ledger = ledger.publish_many(
                adapt_futures_lifecycle_posting(
                    posting,
                    ledger=ledger,
                    contract=selected,
                    policy=inputs.delivery_policy,
                    ctd=ctd,
                )
            )
        final_snapshot = ledger.replay()
        if any(item.instrument_id == selected_id for item in final_snapshot.positions):
            raise PublicProfileError("futures_contract_not_closed")
        provenance_hash = sha256_prefixed(
            {
                "manifest_hash": inputs.continuous_manifest.manifest_hash(),
                "history_hash": inputs.contract_history.content_hash(),
                "selection_hash": selection.receipt_hash(),
                "selected_contract_hash": selected.contract_hash(),
                "margin_hash": margin.result_hash(),
                "ctd_hash": ctd.decision_hash() if ctd is not None else None,
                "lifecycle_hashes": [item.posting_hash() for item in lifecycle],
                "ledger_hash": ledger.content_hash,
                "exposure_hash": exposure.content_hash,
                "scenario_hash": scenario.content_hash,
                "source_document_hashes": list(inputs.source_document_hashes),
            },
            label="public_t02_futures_provenance",
        )
        source_hashes = _sorted_hashes(
            (
                *inputs.source_document_hashes,
                inputs.continuous_manifest.manifest_hash(),
                inputs.contract_history.content_hash(),
                selection.receipt_hash(),
                selected.contract_hash(),
                inputs.margin_source.policy.policy_hash(),
                margin.result_hash(),
                inputs.product_registry.contract_hash(),
                inputs.market_state.state_hash(),
                exposure.content_hash,
                extended.content_hash,
                scenario.content_hash,
                provenance_hash,
                *((ctd.decision_hash(), ctd.basket_hash) if ctd is not None else ()),
            )
        )
        return PublicFuturesProfileReceipt(
            profile_id=PUBLIC_T02_PROFILE_ID,
            fixture_semantics=inputs.fixture_semantics,
            input_hash=_futures_input_hash(inputs),
            continuous_manifest_hash=inputs.continuous_manifest.manifest_hash(),
            contract_history_hash=inputs.contract_history.content_hash(),
            selected_contract_id=selected_id,
            selected_contract_hash=selected.contract_hash(),
            selected_contract_multiplier=selected.contract_multiplier,
            position_quantity=inputs.position_quantity,
            prior_settlement_price=selected_candidate.settlement_price,
            final_settlement_price=inputs.final_settlement_price,
            selection_receipt_hash=selection.receipt_hash(),
            resolved_contract_version=selected.version,
            margin_policy_hash=inputs.margin_source.policy.policy_hash(),
            margin_waterfall_hash=margin.result_hash(),
            variation_margin=margin.variation_margin,
            lifecycle_cash_delta=sum(
                (item.cash_delta for item in lifecycle), start=_ZERO
            ),
            ctd_decision_hash=(ctd.decision_hash() if ctd is not None else None),
            lifecycle_posting_hashes=tuple(
                sorted(item.posting_hash() for item in lifecycle)
            ),
            pre_settlement_ledger_hash=pre_settlement_ledger.content_hash,
            final_ledger_hash=ledger.content_hash,
            final_ledger_head_hash=ledger.head_hash,
            final_ledger_event_hashes=tuple(
                sorted(item.content_hash for item in ledger.events)
            ),
            final_realized_pnl=tuple(
                (item.currency, item.amount) for item in final_snapshot.realized_pnl
            ),
            exposure_hash=exposure.content_hash,
            extended_exposure_hash=extended.content_hash,
            scenario_hash=scenario.content_hash,
            economic_evidence_hash=scenario.economic_evidence.content_hash,
            provenance_hash=provenance_hash,
            source_hashes=source_hashes,
            _factory_token=_RECEIPT_FACTORY_TOKEN,
        )


def run_public_t01_spot_profile(
    inputs: PublicSpotProfileInputs,
) -> PublicSpotProfileReceipt:
    """Execute and mint one source-owned public T-01 receipt."""

    return PublicSpotFuturesConformanceService().run_spot(inputs)


def run_public_t02_futures_profile(
    inputs: PublicFuturesProfileInputs,
) -> PublicFuturesProfileReceipt:
    """Execute and mint one source-owned public T-02 receipt."""

    return PublicSpotFuturesConformanceService().run_futures(inputs)


def _fixture_hash(
    source_document_id: str,
    source_document_hashes: tuple[str, ...],
    label: str,
) -> str:
    return sha256_prefixed(
        {
            "source_document_id": source_document_id,
            "source_document_hashes": list(source_document_hashes),
            "synthetic_fixture_component": label,
        },
        label="public_conformance_synthetic_fixture",
    )


def _fixture_source(
    *,
    source_document_id: str,
    source_document_hashes: tuple[str, ...],
    observed_at: str,
    label: str,
) -> SourceReference:
    return SourceReference(
        source_id=f"public.synthetic.{source_document_id}",
        source_version="v1",
        content_hash=_fixture_hash(source_document_id, source_document_hashes, label),
        observed_at=observed_at,
        source_uri=(
            f"/tmp/codex-gap-closure/public-conformance/{source_document_id}.json"
        ),
    )


def _validate_builder_clocks(
    *,
    observation_at: str,
    valuation_at: str,
    knowledge_at: str,
    minimum_horizon_days: int,
) -> tuple[datetime, datetime, datetime]:
    observation = _instant(observation_at, "builder.observation_at")
    valuation = _instant(valuation_at, "builder.valuation_at")
    knowledge = _instant(knowledge_at, "builder.knowledge_at")
    if not observation <= knowledge <= valuation:
        raise PublicProfileError("builder_clock_order_invalid")
    if valuation - observation < timedelta(days=minimum_horizon_days):
        raise PublicProfileError("builder_fixture_horizon_too_short")
    return observation, valuation, knowledge


def build_public_t01_inputs(
    *,
    source_document_id: str,
    source_document_hashes: tuple[str, ...],
    observation_at: str,
    valuation_at: str,
    knowledge_at: str,
    instrument_id: str,
    currency: str,
    entry_price: Decimal,
    quantity: Decimal,
) -> PublicSpotProfileInputs:
    """Build the canonical synthetic T-01 fixture from public trace economics.

    ``instrument_id``, ``currency``, ``entry_price``, and ``quantity`` are the
    governing public-trace values.  The builder derives all lifecycle, cost,
    exposure, and stress objects from them and never accepts a result receipt.
    """

    _require_hashes(source_document_hashes, "source_document_hashes")
    if (
        not source_document_id
        or entry_price <= _ZERO
        or quantity <= _ZERO
        or len(currency) != 3
        or currency != currency.upper()
    ):
        raise PublicProfileError("t01_builder_canonical_economics_invalid")
    observation, valuation, knowledge = _validate_builder_clocks(
        observation_at=observation_at,
        valuation_at=valuation_at,
        knowledge_at=knowledge_at,
        minimum_horizon_days=10,
    )
    valid = EffectivePeriod(
        _instant_text(observation - timedelta(days=365)),
        _instant_text(valuation + timedelta(days=3650)),
    )
    source = _fixture_source(
        source_document_id=source_document_id,
        source_document_hashes=source_document_hashes,
        observed_at=_instant_text(observation - timedelta(days=1)),
        label="product-master",
    )
    calendar_hash = _fixture_hash(
        source_document_id, source_document_hashes, "calendar"
    )
    calendar = MarketCalendarAuthority(
        schema_version=MARKET_CALENDAR_SCHEMA_VERSION,
        calendar_id="cal_public_t01_continuous",
        calendar_version_id="calv_public_t01_v1",
        version=1,
        market_mode="continuous_24x7",
        timezone_name="UTC",
        tzdb_version="2026a",
        dst_transition_policy=("iana_tzdb_reject_ambiguous_or_nonexistent_local_time"),
        valid_from=(observation - timedelta(days=365)).date().isoformat(),
        valid_to=(valuation + timedelta(days=3650)).date().isoformat(),
        source_uri=("/tmp/codex-gap-closure/public-conformance/t01-calendar.json"),
        source_content_hash=calendar_hash,
        source_schema_hash=_fixture_hash(
            source_document_id, source_document_hashes, "calendar-schema"
        ),
        published_at=_instant_text(observation - timedelta(days=2)),
        observed_at=_instant_text(observation - timedelta(days=1)),
        weekly_sessions=(),
        exceptions=(),
    )
    price_unit = f"{currency}_per_share"
    reciprocal_unit = f"share_per_{currency}"
    units = UnitRegistry(
        registry_id="units.public.t01.v1",
        version="v1",
        definitions=tuple(
            sorted(
                (
                    UnitDefinition(
                        unit_id=price_unit,
                        dimension="price",
                        canonical_unit_id=price_unit,
                        scale_to_canonical=_ONE,
                        version="v1",
                        source_hash=_fixture_hash(
                            source_document_id,
                            source_document_hashes,
                            "unit-price",
                        ),
                    ),
                    UnitDefinition(
                        unit_id="lot",
                        dimension="quantity",
                        canonical_unit_id="share",
                        scale_to_canonical=Decimal("10"),
                        version="v1",
                        source_hash=_fixture_hash(
                            source_document_id,
                            source_document_hashes,
                            "unit-lot",
                        ),
                    ),
                    UnitDefinition(
                        unit_id=reciprocal_unit,
                        dimension="price",
                        canonical_unit_id=price_unit,
                        scale_to_canonical=_ONE,
                        version="v1",
                        source_hash=_fixture_hash(
                            source_document_id,
                            source_document_hashes,
                            "unit-reciprocal",
                        ),
                    ),
                    UnitDefinition(
                        unit_id="share",
                        dimension="quantity",
                        canonical_unit_id="share",
                        scale_to_canonical=_ONE,
                        version="v1",
                        source_hash=_fixture_hash(
                            source_document_id,
                            source_document_hashes,
                            "unit-share",
                        ),
                    ),
                ),
                key=lambda item: item.unit_id,
            )
        ),
    )
    service = ProviderNormalizationService(
        calendar_registry=CalendarRegistry(
            registry_id="calendars.public.t01.v1",
            version="v1",
            calendars=(calendar,),
        ),
        unit_registry=units,
        adapters=(
            EpochScaledReciprocalAdapter(),
            IsoLocalDirectAdapter(),
        ),
    )
    publication = _instant_text(observation + timedelta(seconds=1))
    availability = _instant_text(observation + timedelta(seconds=2))
    ingestion = _instant_text(observation + timedelta(seconds=3))
    direct_row = ProviderRow(
        row_id="row.public.t01.direct",
        provider_id="provider.public.direct",
        provider_version="v1",
        schema_id="public.direct.v1",
        source_object_id="object.public.t01.direct",
        collection_batch_id="batch.public.t01",
        source_artifact_hash=_fixture_hash(
            source_document_id, source_document_hashes, "provider-direct-artifact"
        ),
        source_schema_hash=_fixture_hash(
            source_document_id, source_document_hashes, "provider-direct-schema"
        ),
        ingested_at=ingestion,
        fields={
            "symbol": instrument_id,
            "local_timestamp": observation.replace(tzinfo=None).isoformat(),
            "published_at": publication,
            "available_at": availability,
            "session_id": "CONTINUOUS",
            "price": _decimal_text(entry_price),
            "quantity": _decimal_text(quantity),
        },
    )

    def milliseconds(value: datetime) -> str:
        return str(int(value.timestamp() * 1000))

    reciprocal_raw = Decimal("100") / entry_price
    epoch_row = ProviderRow(
        row_id="row.public.t01.reciprocal",
        provider_id="provider.public.reciprocal",
        provider_version="v1",
        schema_id="public.reciprocal.v1",
        source_object_id="object.public.t01.reciprocal",
        collection_batch_id="batch.public.t01",
        source_artifact_hash=_fixture_hash(
            source_document_id,
            source_document_hashes,
            "provider-reciprocal-artifact",
        ),
        source_schema_hash=_fixture_hash(
            source_document_id,
            source_document_hashes,
            "provider-reciprocal-schema",
        ),
        ingested_at=ingestion,
        fields={
            "ticker": instrument_id,
            "event_ms": milliseconds(observation),
            "publication_ms": milliseconds(observation + timedelta(seconds=1)),
            "availability_ms": milliseconds(observation + timedelta(seconds=2)),
            "session_code": "CONTINUOUS",
            "reciprocal_price_scaled": _decimal_text(reciprocal_raw),
            "size_lots": _decimal_text(quantity / Decimal("10")),
        },
    )

    def normalization_policy(
        provider_id: str,
        *,
        reciprocal: bool,
    ) -> ProviderNormalizationPolicy:
        return ProviderNormalizationPolicy(
            policy_id=f"normalize.{provider_id}.v1",
            version="v1",
            provider_id=provider_id,
            instrument_id=instrument_id,
            provider_symbol=instrument_id,
            calendar_id=calendar.calendar_id,
            exchange_session_id="CONTINUOUS",
            source_timezone="UTC",
            source_price_unit=reciprocal_unit if reciprocal else price_unit,
            target_price_unit=price_unit,
            source_quantity_unit="lot" if reciprocal else "share",
            target_quantity_unit="share",
            currency=currency,
            contract_multiplier=_ONE,
            price_scale=Decimal("0.01") if reciprocal else _ONE,
            quantity_scale=_ONE,
            quote_convention=(
                QuoteConvention.RECIPROCAL if reciprocal else QuoteConvention.DIRECT
            ),
            corporate_action_adjustment=CorporateActionAdjustment.RAW,
            missing_value_semantics=MissingValueSemantics.REJECT_ROW,
        )

    underlying_id = f"underlying.{instrument_id}"
    issuer_id = f"issuer.{instrument_id}"
    listing_id = f"listing.{instrument_id}"
    rights_id = f"{instrument_id}.rights"
    subscribed_id = f"{instrument_id}.subscribed"
    successor_id = f"{instrument_id}.successor"
    borrow_id = f"{instrument_id}.borrow"
    underlying = EconomicUnderlying(
        underlying_id=underlying_id,
        name=f"Synthetic conformance underlying for {instrument_id}",
        asset_class="equity",
        unit="share",
        currency=currency,
        validity=valid,
        source=source,
    )
    issuer = Issuer(
        issuer_id=issuer_id,
        legal_name=f"Synthetic conformance issuer {instrument_id}",
        jurisdiction="US",
        validity=valid,
        source=source,
    )
    instruments = (
        Instrument(
            instrument_id=instrument_id,
            kind=InstrumentKind.SPOT,
            name=f"{instrument_id} primary spot",
            economic_underlying_id=underlying_id,
            issuer_id=issuer_id,
            currency=currency,
            unit="share",
            validity=valid,
            source=source,
            primary_listing_id=listing_id,
        ),
        Instrument(
            instrument_id=borrow_id,
            kind=InstrumentKind.SPOT,
            name=f"{instrument_id} borrow comparison share",
            economic_underlying_id=underlying_id,
            issuer_id=issuer_id,
            currency=currency,
            unit="share",
            validity=valid,
            source=source,
        ),
        Instrument(
            instrument_id=rights_id,
            kind=InstrumentKind.SPOT,
            name=f"{instrument_id} subscription right",
            economic_underlying_id=underlying_id,
            issuer_id=issuer_id,
            currency=currency,
            unit="share",
            validity=valid,
            source=source,
        ),
        Instrument(
            instrument_id=successor_id,
            kind=InstrumentKind.SPOT,
            name=f"{instrument_id} merger successor",
            economic_underlying_id=underlying_id,
            issuer_id=issuer_id,
            currency=currency,
            unit="share",
            validity=valid,
            source=source,
        ),
        Instrument(
            instrument_id=subscribed_id,
            kind=InstrumentKind.SPOT,
            name=f"{instrument_id} subscribed share lot",
            economic_underlying_id=underlying_id,
            issuer_id=issuer_id,
            currency=currency,
            unit="share",
            validity=valid,
            source=source,
        ),
    )
    listing = Listing(
        listing_id=listing_id,
        instrument_id=instrument_id,
        venue_mic="XOFF",
        symbol=instrument_id,
        trading_currency=currency,
        price_unit=price_unit,
        quantity_unit="share",
        calendar_id=calendar.calendar_id,
        validity=valid,
        source=source,
        market_segment="OFF_EXCHANGE_REFERENCE",
        session_id="CONTINUOUS",
    )
    lei = f"LEI{_fixture_hash(source_document_id, source_document_hashes, 'lei')[-17:]}"
    identifier = IssuerIdentifierRevision(
        identifier_id=f"issuer-id.{instrument_id}.lei",
        revision=1,
        issuer_id=issuer_id,
        namespace=IssuerIdentifierNamespace.LEI,
        value=lei,
        jurisdiction="US",
        validity=valid,
        knowledge_at=_instant_text(observation - timedelta(hours=12)),
        source=source,
    )
    aliases = (
        SymbolAlias(
            alias_id=f"alias.{instrument_id}.direct",
            instrument_id=instrument_id,
            listing_id=listing_id,
            provider_id="provider.public.direct",
            symbol=instrument_id,
            validity=valid,
            source=source,
        ),
        SymbolAlias(
            alias_id=f"alias.{instrument_id}.reciprocal",
            instrument_id=instrument_id,
            listing_id=listing_id,
            provider_id="provider.public.reciprocal",
            symbol=instrument_id,
            validity=valid,
            source=source,
        ),
    )
    registry = InstrumentRegistry(
        economic_underlyings=(underlying,),
        issuers=(issuer,),
        issuer_identifier_revisions=(identifier,),
        instruments=tuple(sorted(instruments, key=lambda item: item.instrument_id)),
        listings=(listing,),
        symbol_aliases=aliases,
    )
    universe_hash = _fixture_hash(
        source_document_id, source_document_hashes, "pit-universe"
    )
    universe = PointInTimeSpotUniverse(
        (
            UniverseMembership(
                universe_id="public.t01.universe",
                instrument_id=instrument_id,
                effective_from=observation,
                effective_to=None,
                announcement_at=observation - timedelta(days=1),
                implementation_at=observation,
                known_at=observation,
                membership_source_hash=universe_hash,
            ),
        )
    )
    short_quantity = max(quantity / Decimal("10"), _ONE)
    initial_book = SpotBook(
        positions=tuple(
            sorted(
                (
                    SpotPosition(
                        instrument_id=instrument_id,
                        quantity=quantity,
                        total_cost_basis=quantity * entry_price,
                        currency=currency,
                    ),
                    SpotPosition(
                        instrument_id=borrow_id,
                        quantity=-short_quantity,
                        total_cost_basis=(
                            short_quantity * entry_price * Decimal("0.8")
                        ),
                        currency=currency,
                    ),
                ),
                key=lambda item: item.instrument_id,
            )
        ),
        cash=(
            CashBalance(
                currency=currency,
                amount=quantity * entry_price * Decimal("5"),
            ),
        ),
    )
    action_policy_hash = _fixture_hash(
        source_document_id, source_document_hashes, "corporate-action-policy"
    )
    rights_effective = observation + timedelta(days=4)
    subscription_effective = observation + timedelta(days=5)
    merger_effective = observation + timedelta(days=6)
    actions = (
        CorporateAction(
            action_id=f"action.{instrument_id}.rights",
            revision=1,
            action_type=CorporateActionType.RIGHTS_ISSUE,
            instrument_id=instrument_id,
            announced_at=observation + timedelta(days=1),
            known_at=observation + timedelta(days=1, hours=1),
            record_at=observation + timedelta(days=2),
            ex_at=observation + timedelta(days=3),
            payment_at=rights_effective,
            effective_at=rights_effective,
            source_id="source.synthetic.exchange",
            source_record_hash=_fixture_hash(
                source_document_id, source_document_hashes, "rights-action"
            ),
            currency=currency,
            ratio=Decimal("0.1"),
            rights_instrument_id=rights_id,
            subscription_price=entry_price * Decimal("0.8"),
            terms_policy_hash=action_policy_hash,
        ),
        CorporateAction(
            action_id=f"action.{instrument_id}.subscription",
            revision=1,
            action_type=CorporateActionType.RIGHTS_SUBSCRIPTION,
            instrument_id=rights_id,
            announced_at=rights_effective,
            known_at=rights_effective + timedelta(hours=1),
            record_at=None,
            ex_at=None,
            payment_at=None,
            effective_at=subscription_effective,
            source_id="source.synthetic.exchange",
            source_record_hash=_fixture_hash(
                source_document_id, source_document_hashes, "subscription-action"
            ),
            currency=currency,
            ratio=_ONE,
            subscription_price=entry_price * Decimal("0.8"),
            subscription_fraction=_ONE,
            replacement_instrument_id=subscribed_id,
            terms_policy_hash=action_policy_hash,
        ),
        CorporateAction(
            action_id=f"action.{instrument_id}.cash-stock-merger",
            revision=1,
            action_type=CorporateActionType.MERGER,
            instrument_id=instrument_id,
            announced_at=subscription_effective,
            known_at=subscription_effective + timedelta(hours=1),
            record_at=None,
            ex_at=None,
            payment_at=None,
            effective_at=merger_effective,
            source_id="source.synthetic.exchange",
            source_record_hash=_fixture_hash(
                source_document_id, source_document_hashes, "merger-action"
            ),
            currency=currency,
            cash_per_share=entry_price * Decimal("0.1"),
            ratio=Decimal("0.5"),
            tax_rate=Decimal("0.1"),
            replacement_instrument_id=successor_id,
            cash_basis_fraction=Decimal("0.2"),
            terms_policy_hash=action_policy_hash,
        ),
    )
    recall = BorrowRecall(
        recall_id=f"recall.{borrow_id}",
        revision=1,
        instrument_id=borrow_id,
        reason=BorrowRecallReason.LENDER_RECALL,
        announced_at=observation + timedelta(days=6),
        known_at=observation + timedelta(days=6, hours=1),
        effective_at=observation + timedelta(days=7),
        cover_deadline_at=observation + timedelta(days=8),
        recalled_quantity=short_quantity / Decimal("2"),
        penalty_per_unit=entry_price * Decimal("0.01"),
        source_hash=_fixture_hash(
            source_document_id, source_document_hashes, "borrow-recall"
        ),
    )
    market_metadata = ObservationMetadata(
        observed_at=_instant_text(valuation),
        knowledge_at=_instant_text(valuation),
        source_hash=_fixture_hash(
            source_document_id, source_document_hashes, "market-state"
        ),
        calendar_id=calendar.calendar_id,
        max_age_seconds=0,
    )
    successor_price = entry_price * Decimal("2.1")
    subscribed_price = entry_price
    borrow_price = entry_price * Decimal("0.9")
    market_state = MarketState(
        state_id="public.t01.market.state",
        valuation_at=_instant_text(valuation),
        base_currency=currency,
        calendar_ids=(calendar.calendar_id,),
        spots=(
            SpotQuote(
                instrument_id=borrow_id,
                price=borrow_price,
                currency=currency,
                unit=price_unit,
                metadata=market_metadata,
            ),
            SpotQuote(
                instrument_id=instrument_id,
                price=entry_price,
                currency=currency,
                unit=price_unit,
                metadata=market_metadata,
            ),
            SpotQuote(
                instrument_id=successor_id,
                price=successor_price,
                currency=currency,
                unit=price_unit,
                metadata=market_metadata,
            ),
            SpotQuote(
                instrument_id=subscribed_id,
                price=subscribed_price,
                currency=currency,
                unit=price_unit,
                metadata=market_metadata,
            ),
        ),
        liquidity_quotes=(
            LiquidityQuote(
                instrument_id=borrow_id,
                currency=currency,
                bid=borrow_price * Decimal("0.999"),
                ask=borrow_price * Decimal("1.001"),
                price_unit=price_unit,
                depth_quantity=quantity * Decimal("10"),
                quantity_unit="share",
                metadata=market_metadata,
            ),
            LiquidityQuote(
                instrument_id=successor_id,
                currency=currency,
                bid=successor_price * Decimal("0.999"),
                ask=successor_price * Decimal("1.001"),
                price_unit=price_unit,
                depth_quantity=quantity * Decimal("10"),
                quantity_unit="share",
                metadata=market_metadata,
            ),
            LiquidityQuote(
                instrument_id=subscribed_id,
                currency=currency,
                bid=subscribed_price * Decimal("0.999"),
                ask=subscribed_price * Decimal("1.001"),
                price_unit=price_unit,
                depth_quantity=quantity * Decimal("10"),
                quantity_unit="share",
                metadata=market_metadata,
            ),
        ),
    )
    cost_source_hash = _fixture_hash(
        source_document_id, source_document_hashes, "empirical-cost-sample"
    )
    cost_model_hash = _fixture_hash(
        source_document_id, source_document_hashes, "empirical-cost-model"
    )
    daily_capacity = quantity * Decimal("2")
    quantity_grid = (
        quantity * Decimal("0.1"),
        quantity * Decimal("0.2"),
    )
    calibration = EmpiricalImpactCalibration(
        calibration_id=f"empirical.{instrument_id}.normal",
        instrument_id=instrument_id,
        instrument_kind="SPOT",
        currency=currency,
        sample_window_start=_instant_text(valuation - timedelta(days=365)),
        sample_window_end=_instant_text(valuation - timedelta(days=2)),
        known_at=_instant_text(valuation - timedelta(days=1)),
        valid_until=_instant_text(valuation + timedelta(days=30)),
        regime_id="normal",
        sample_size=500,
        minimum_participation_rate=Decimal("0.01"),
        maximum_participation_rate=Decimal("0.5"),
        minimum_daily_volatility=Decimal("0.005"),
        maximum_daily_volatility=Decimal("0.1"),
        minimum_half_spread_bps=Decimal("0.1"),
        maximum_half_spread_bps=Decimal("20"),
        intercept_bps=Decimal("1"),
        square_root_participation_coefficient=Decimal("4"),
        volatility_coefficient=Decimal("10"),
        spread_coefficient=Decimal("0.5"),
        leg_interaction_coefficient=Decimal("2"),
        holdout_error_bps=Decimal("2"),
        model_hash=cost_model_hash,
        source_hashes=(cost_source_hash,),
    )
    return PublicSpotProfileInputs(
        fixture_semantics=(
            InputFixtureSemantics.EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE
        ),
        source_document_id=source_document_id,
        source_document_hashes=source_document_hashes,
        normalization_service=service,
        provider_conventions=(
            ProviderConventionSource(
                row=direct_row,
                policy=normalization_policy("provider.public.direct", reciprocal=False),
                adapter_id="provider.iso-local-direct",
            ),
            ProviderConventionSource(
                row=epoch_row,
                policy=normalization_policy(
                    "provider.public.reciprocal", reciprocal=True
                ),
                adapter_id="provider.epoch-scaled-reciprocal",
            ),
        ),
        product_registry=registry,
        issuer_query=IssuerIdentifierQuery(
            namespace=IssuerIdentifierNamespace.LEI,
            value=lei,
            jurisdiction="US",
        ),
        universe=universe,
        universe_id="public.t01.universe",
        universe_source_hashes=(universe_hash,),
        effective_at=availability,
        knowledge_at=_instant_text(knowledge),
        opened_at=_instant_text(observation - timedelta(hours=1)),
        initial_book=initial_book,
        initial_book_source_hash=_fixture_hash(
            source_document_id, source_document_hashes, "initial-spot-book"
        ),
        corporate_actions=actions,
        borrow_recalls=(recall,),
        borrow_recall_id=recall.recall_id,
        recall_applied_at=_instant_text(observation + timedelta(days=7)),
        recall_execution_price=borrow_price,
        recall_execution_quote_hash=_fixture_hash(
            source_document_id, source_document_hashes, "recall-execution-quote"
        ),
        recall_commission_per_unit=entry_price * Decimal("0.001"),
        cost_source=EmpiricalCostCapacitySource(
            calibrations=(calibration,),
            policy=EmpiricalCalibrationPolicy(
                policy_id="public.t01.empirical.acceptance",
                version="1",
                minimum_sample_size=100,
                maximum_holdout_error_bps=Decimal("5"),
            ),
            observed_at=_instant_text(valuation),
            regime_id="normal",
            quantity_grid=quantity_grid,
            daily_capacity_quantity=daily_capacity,
            daily_volatility=Decimal("0.02"),
            half_spread_bps=Decimal("5"),
            margin_requirement=_ZERO,
            margin_funding_bps=Decimal("10"),
            gross_edge_bps=Decimal("500"),
            target_degradation_fraction=Decimal("0.1"),
            source_hashes=(cost_source_hash,),
        ),
        market_state=market_state,
        economic_policy=EconomicProjectionPolicy(
            policy_id="public.t01.economic.constraints",
            version="1",
            maximum_absolute_basis_fraction=Decimal("0.25"),
            maximum_volatility_curvature=Decimal("0.25"),
        ),
        stress_return=Decimal("-0.15"),
        liquidity_haircut=Decimal("0.25"),
        liquidity_cost_multiplier=Decimal("1.5"),
        claimed_instrument_id=instrument_id,
        claimed_issuer_id=issuer_id,
        claimed_listing_id=listing_id,
    )


def _futures_master(
    *,
    source_document_id: str,
    source_document_hashes: tuple[str, ...],
    contract_id: str,
    root_id: str,
    economic_underlying_id: str,
    currency: str,
    multiplier: Decimal,
    listed_at: datetime,
    last_trade_at: datetime,
    final_settlement_at: datetime,
    knowledge_at: datetime,
    settlement_mode: FuturesSettlementMode,
    deliverable_basket_id: str | None,
    version: int = 1,
    supersedes_hash: str | None = None,
    daily_price_limit: Decimal = Decimal("10"),
) -> FuturesContractMasterVersion:
    physical = settlement_mode is FuturesSettlementMode.PHYSICAL
    return FuturesContractMasterVersion(
        record_id=f"master.{contract_id}",
        version=version,
        contract_id=contract_id,
        root_id=root_id,
        economic_underlying_id=economic_underlying_id,
        exchange_mic="XOFF",
        contract_month=last_trade_at.strftime("%Y-%m"),
        listed_at=_instant_text(listed_at),
        last_trade_at=_instant_text(last_trade_at),
        first_notice_at=(
            _instant_text(last_trade_at - timedelta(days=10)) if physical else None
        ),
        final_settlement_at=_instant_text(final_settlement_at),
        delivery_start_at=(_instant_text(final_settlement_at) if physical else None),
        delivery_end_at=(
            _instant_text(final_settlement_at + timedelta(days=5)) if physical else None
        ),
        contract_multiplier=multiplier,
        contract_unit="index_point",
        minimum_tick=Decimal("0.01"),
        tick_value=Decimal("0.01") * multiplier,
        trading_currency=currency,
        settlement_currency=currency,
        settlement_mode=settlement_mode,
        settlement_formula_id="formula.public.reviewed.v1",
        calendar_id="cal_public_t02",
        session_id="session_public_t02",
        daily_price_limit=daily_price_limit,
        margin_policy_id="margin.public.t02.v1",
        deliverable_basket_id=deliverable_basket_id,
        metadata=FuturesTermsMetadata(
            valid_from=_instant_text(listed_at),
            valid_to=None,
            knowledge_at=_instant_text(knowledge_at),
            source_id="source.public.reviewed.terms",
            source_version=f"v{version}",
            source_hash=_fixture_hash(
                source_document_id,
                source_document_hashes,
                f"contract-{contract_id}-v{version}",
            ),
            policy_version=f"v{version}",
        ),
        supersedes_hash=supersedes_hash,
    )


def build_public_t02_inputs(
    *,
    source_document_id: str,
    source_document_hashes: tuple[str, ...],
    observation_at: str,
    valuation_at: str,
    knowledge_at: str,
    underlying_instrument_id: str,
    root_id: str,
    near_contract_id: str,
    selected_contract_id: str,
    currency: str,
    entry_price: Decimal,
    final_price: Decimal,
    multiplier: Decimal,
    quantity: Decimal,
    settlement_mode: FuturesSettlementMode = FuturesSettlementMode.CASH,
) -> PublicFuturesProfileInputs:
    """Build a canonical synthetic T-02 fixture from public trace economics."""

    _require_hashes(source_document_hashes, "source_document_hashes")
    if (
        not source_document_id
        or near_contract_id == selected_contract_id
        or entry_price <= _ZERO
        or final_price <= _ZERO
        or multiplier <= _ZERO
        or quantity == _ZERO
        or len(currency) != 3
        or currency != currency.upper()
        or not isinstance(settlement_mode, FuturesSettlementMode)
    ):
        raise PublicProfileError("t02_builder_canonical_economics_invalid")
    observation, valuation, knowledge = _validate_builder_clocks(
        observation_at=observation_at,
        valuation_at=valuation_at,
        knowledge_at=knowledge_at,
        minimum_horizon_days=1,
    )
    listed = valuation - timedelta(days=365)
    near_last = valuation + timedelta(days=12)
    near_final = near_last + timedelta(days=1)
    selected_last = valuation + timedelta(days=120)
    selected_final = selected_last + timedelta(days=1)
    basket_id = (
        f"basket.{selected_contract_id}"
        if settlement_mode is FuturesSettlementMode.PHYSICAL
        else None
    )
    terms_v1_known = observation - timedelta(days=30)
    near = _futures_master(
        source_document_id=source_document_id,
        source_document_hashes=source_document_hashes,
        contract_id=near_contract_id,
        root_id=root_id,
        economic_underlying_id=f"underlying.{root_id}",
        currency=currency,
        multiplier=multiplier,
        listed_at=listed,
        last_trade_at=near_last,
        final_settlement_at=near_final,
        knowledge_at=terms_v1_known,
        settlement_mode=settlement_mode,
        deliverable_basket_id=(
            f"basket.{near_contract_id}"
            if settlement_mode is FuturesSettlementMode.PHYSICAL
            else None
        ),
    )
    selected_v1 = _futures_master(
        source_document_id=source_document_id,
        source_document_hashes=source_document_hashes,
        contract_id=selected_contract_id,
        root_id=root_id,
        economic_underlying_id=f"underlying.{root_id}",
        currency=currency,
        multiplier=multiplier,
        listed_at=listed,
        last_trade_at=selected_last,
        final_settlement_at=selected_final,
        knowledge_at=terms_v1_known,
        settlement_mode=settlement_mode,
        deliverable_basket_id=basket_id,
    )
    selected_v2 = _futures_master(
        source_document_id=source_document_id,
        source_document_hashes=source_document_hashes,
        contract_id=selected_contract_id,
        root_id=root_id,
        economic_underlying_id=f"underlying.{root_id}",
        currency=currency,
        multiplier=multiplier,
        listed_at=listed,
        last_trade_at=selected_last,
        final_settlement_at=selected_final,
        knowledge_at=knowledge,
        settlement_mode=settlement_mode,
        deliverable_basket_id=basket_id,
        version=2,
        supersedes_hash=selected_v1.contract_hash(),
        daily_price_limit=Decimal("9"),
    )
    history = FuturesContractMasterHistory((near, selected_v1, selected_v2))
    near_quote_hash = _fixture_hash(
        source_document_id, source_document_hashes, "near-futures-quote"
    )
    selected_quote_hash = _fixture_hash(
        source_document_id, source_document_hashes, "selected-futures-quote"
    )
    candidates = (
        FuturesSelectionCandidate(
            contract=near,
            observed_at=_instant_text(observation),
            knowledge_at=_instant_text(knowledge),
            bid=entry_price - Decimal("0.10"),
            ask=entry_price + Decimal("0.10"),
            settlement_price=entry_price,
            volume=Decimal("1000"),
            open_interest=Decimal("5000"),
            source_quote_hash=near_quote_hash,
        ),
        FuturesSelectionCandidate(
            contract=selected_v1,
            observed_at=_instant_text(observation),
            knowledge_at=_instant_text(knowledge),
            bid=entry_price - Decimal("0.01"),
            ask=entry_price + Decimal("0.01"),
            settlement_price=entry_price,
            volume=Decimal("10000"),
            open_interest=Decimal("50000"),
            source_quote_hash=selected_quote_hash,
        ),
    )
    contract_ids = tuple(sorted((near_contract_id, selected_contract_id)))
    manifest = ContinuousSeriesManifest(
        series_id=f"continuous.{root_id}",
        root_id=root_id,
        source_contract_ids=contract_ids,
        roll_rule_id="roll.volume_oi_notice.v1",
        roll_window_days=5,
        liquidity_rule_id="liquidity.volume_oi_spread.v1",
        delivery_avoidance_rule_id="delivery.before_notice.v1",
        adjustment_method=RollAdjustmentMethod.DIFFERENCE,
        adjustment_values=tuple((contract_id, _ZERO) for contract_id in contract_ids),
        builder_version="builder.public.v1",
        source_snapshot_hashes=tuple(sorted((near_quote_hash, selected_quote_hash))),
        generated_series_hash=_fixture_hash(
            source_document_id, source_document_hashes, "continuous-series"
        ),
        generated_at=_instant_text(valuation),
    )
    valid = EffectivePeriod(
        _instant_text(listed),
        _instant_text(selected_final + timedelta(days=365)),
    )
    source = _fixture_source(
        source_document_id=source_document_id,
        source_document_hashes=source_document_hashes,
        observed_at=_instant_text(terms_v1_known),
        label="futures-product-master",
    )
    economic_underlying_id = f"underlying.{root_id}"
    underlying = EconomicUnderlying(
        underlying_id=economic_underlying_id,
        name=f"Synthetic conformance underlying {root_id}",
        asset_class="index",
        unit="index_point",
        currency=currency,
        validity=valid,
        source=source,
    )
    underlying_instrument = Instrument(
        instrument_id=underlying_instrument_id,
        kind=InstrumentKind.SPOT,
        name=f"{root_id} underlying spot reference",
        economic_underlying_id=economic_underlying_id,
        currency=currency,
        unit="index_point",
        validity=valid,
        source=source,
    )
    future_instrument = Instrument(
        instrument_id=selected_contract_id,
        kind=InstrumentKind.FUTURE,
        name=f"{selected_contract_id} actual future",
        economic_underlying_id=economic_underlying_id,
        currency=currency,
        unit="contract",
        validity=valid,
        source=source,
    )
    specification = ContractSpecification(
        contract_specification_id=f"spec.{selected_contract_id}",
        instrument_id=selected_contract_id,
        contract_multiplier=multiplier,
        contract_unit="index_point",
        settlement_type=(
            SettlementType.PHYSICAL
            if settlement_mode is FuturesSettlementMode.PHYSICAL
            else SettlementType.CASH
        ),
        settlement_currency=currency,
        expiry_at=_instant_text(selected_final),
        last_trade_at=_instant_text(selected_last),
        minimum_tick=Decimal("0.01"),
        tick_value=Decimal("0.01") * multiplier,
        trading_currency=currency,
        calendar_id="cal_public_t02",
        lifecycle_rule_id="future.public.settlement.v1",
        validity=valid,
        source=source,
    )
    registry = InstrumentRegistry(
        economic_underlyings=(underlying,),
        issuers=(),
        instruments=(future_instrument, underlying_instrument),
        listings=(),
        contract_specifications=(specification,),
        relationships=(
            InstrumentRelationship(
                relationship_id=f"relationship.{selected_contract_id}.underlying",
                source_instrument_id=selected_contract_id,
                target_instrument_id=underlying_instrument_id,
                relationship_type=InstrumentRelationshipType.FUTURE_UNDERLYING,
                validity=valid,
                source=source,
            ),
        ),
    )
    market_source_hash = _fixture_hash(
        source_document_id, source_document_hashes, "futures-market-state"
    )
    metadata = ObservationMetadata(
        observed_at=_instant_text(observation),
        knowledge_at=_instant_text(knowledge),
        source_hash=market_source_hash,
        calendar_id="cal_public_t02",
        max_age_seconds=int((valuation - observation).total_seconds()) + 1,
    )
    underlying_price = entry_price * Decimal("0.99")
    market_quote = FuturesContractQuote(
        contract_id=selected_contract_id,
        underlying_instrument_id=underlying_instrument_id,
        expiry_at=_instant_text(selected_final),
        currency=currency,
        price_unit=f"{currency}_per_index_point",
        bid=entry_price - Decimal("0.01"),
        ask=entry_price + Decimal("0.01"),
        last=entry_price,
        settlement=entry_price,
        bid_size=Decimal("100"),
        ask_size=Decimal("100"),
        volume=Decimal("10000"),
        open_interest=Decimal("50000"),
        condition=QuoteCondition.OFFICIAL_SETTLEMENT,
        initial_margin_per_contract=entry_price * multiplier * Decimal("0.1"),
        collateral_per_contract=entry_price * multiplier * Decimal("0.05"),
        margin_model_hash=_fixture_hash(
            source_document_id, source_document_hashes, "margin-model"
        ),
        metadata=metadata,
    )
    market_state = MarketState(
        state_id="public.t02.market.state",
        valuation_at=_instant_text(valuation),
        base_currency=currency,
        calendar_ids=("cal_public_t02",),
        spots=(
            SpotQuote(
                instrument_id=underlying_instrument_id,
                price=underlying_price,
                currency=currency,
                unit=f"{currency}_per_index_point",
                metadata=metadata,
            ),
        ),
        liquidity_quotes=(
            LiquidityQuote(
                instrument_id=selected_contract_id,
                currency=currency,
                bid=entry_price - Decimal("0.01"),
                ask=entry_price + Decimal("0.01"),
                price_unit=f"{currency}_per_index_point",
                depth_quantity=Decimal("100"),
                quantity_unit="contract",
                metadata=metadata,
            ),
        ),
        futures_curves=(
            FuturesCurveState(
                curve_id=f"curve.{root_id}",
                underlying_instrument_id=underlying_instrument_id,
                currency=currency,
                price_unit=f"{currency}_per_index_point",
                contracts=(market_quote,),
                metadata=metadata,
            ),
        ),
    )
    margin_policy = FuturesMarginPolicyVersion(
        policy_id="margin.public.t02.v1",
        version=1,
        exchange_mic="XOFF",
        currency=currency,
        initial_per_contract=entry_price * multiplier * Decimal("0.1"),
        maintenance_per_contract=entry_price * multiplier * Decimal("0.08"),
        variation_frequency="DAILY",
        collateral=(
            CollateralEligibility(
                asset_id=f"collateral.cash.{currency.lower()}",
                kind=CollateralAssetKind.CASH,
                currency=currency,
                haircut=_ZERO,
                concentration_limit=_ONE,
                source_hash=_fixture_hash(
                    source_document_id,
                    source_document_hashes,
                    "collateral-rule",
                ),
            ),
        ),
        collateral_waterfall=(f"collateral.cash.{currency.lower()}",),
        collateral_yield_rate=Decimal("0.02"),
        spread_offset_rate=Decimal("0.5"),
        additional_funding_allowed=True,
        metadata=FuturesTermsMetadata(
            valid_from=_instant_text(listed),
            valid_to=None,
            knowledge_at=_instant_text(terms_v1_known),
            source_id="source.public.reviewed.margin",
            source_version="v1",
            source_hash=_fixture_hash(
                source_document_id, source_document_hashes, "margin-policy"
            ),
            policy_version="v1",
        ),
    )
    basket = None
    if settlement_mode is FuturesSettlementMode.PHYSICAL:
        assert basket_id is not None
        basket = DeliverableBasket(
            basket_id=basket_id,
            version="v1",
            contract_root_id=root_id,
            delivery_unit=multiplier,
            grades=(
                DeliverableGrade(
                    grade_id=f"grade.{selected_contract_id}.a",
                    instrument_id=f"deliverable.{selected_contract_id}.a",
                    location_id="location.public",
                    clean_cash_price=final_price * Decimal("1.01"),
                    accrued_interest=Decimal("0.5"),
                    conversion_factor=Decimal("1.01"),
                    quality_adjustment=_ZERO,
                    location_adjustment=_ZERO,
                    delivery_cost=Decimal("1"),
                    source_hash=_fixture_hash(
                        source_document_id,
                        source_document_hashes,
                        "deliverable-grade-a",
                    ),
                ),
                DeliverableGrade(
                    grade_id=f"grade.{selected_contract_id}.b",
                    instrument_id=f"deliverable.{selected_contract_id}.b",
                    location_id="location.public",
                    clean_cash_price=final_price * Decimal("1.02"),
                    accrued_interest=Decimal("0.4"),
                    conversion_factor=_ONE,
                    quality_adjustment=_ZERO,
                    location_adjustment=_ZERO,
                    delivery_cost=Decimal("2"),
                    source_hash=_fixture_hash(
                        source_document_id,
                        source_document_hashes,
                        "deliverable-grade-b",
                    ),
                ),
            ),
            valid_from=_instant_text(valuation),
            valid_to=_instant_text(selected_final + timedelta(days=10)),
            knowledge_at=_instant_text(knowledge),
            source_hash=_fixture_hash(
                source_document_id, source_document_hashes, "deliverable-basket"
            ),
        )
    return PublicFuturesProfileInputs(
        fixture_semantics=(
            InputFixtureSemantics.EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE
        ),
        source_document_id=source_document_id,
        source_document_hashes=source_document_hashes,
        continuous_manifest=manifest,
        contract_history=history,
        quote_candidates=candidates,
        selection_policy=ContractSelectionPolicy(
            policy_id="public.t02.actual.selection",
            version="v1",
            minimum_days_to_notice=5,
            minimum_days_to_last_trade=3,
            minimum_volume=Decimal("100"),
            minimum_open_interest=Decimal("1000"),
            maximum_spread=Decimal("0.5"),
        ),
        root_id=root_id,
        decision_at=_instant_text(valuation),
        knowledge_at=_instant_text(knowledge),
        opened_at=_instant_text(valuation - timedelta(hours=2)),
        position_quantity=quantity,
        initial_cash=abs(quantity) * entry_price * multiplier * Decimal("5"),
        initial_funding_source_hash=_fixture_hash(
            source_document_id, source_document_hashes, "initial-futures-funding"
        ),
        margin_source=FuturesMarginSource(
            policy=margin_policy,
            collateral_holdings=(
                CollateralHolding(
                    asset_id=f"collateral.cash.{currency.lower()}",
                    market_value=(
                        abs(quantity) * entry_price * multiplier * Decimal("0.05")
                    ),
                ),
            ),
            outright_contracts=abs(quantity),
            spread_contract_pairs=_ZERO,
            variation_margin=-(
                abs(quantity) * entry_price * multiplier * Decimal("0.01")
            ),
            elapsed_days=_ONE,
            occurred_at=_instant_text(valuation),
        ),
        final_settlement_price=final_price,
        final_settlement_at=_instant_text(selected_final),
        final_settlement_source_hash=_fixture_hash(
            source_document_id, source_document_hashes, "final-settlement"
        ),
        delivery_policy=DeliveryPolicy(
            policy_id="public.t02.delivery",
            version="v1",
            physical_delivery_enabled=(
                settlement_mode is FuturesSettlementMode.PHYSICAL
            ),
            close_before_notice_days=5,
            default_closeout_penalty_rate=Decimal("0.02"),
        ),
        deliverable_basket=basket,
        product_registry=registry,
        market_state=market_state,
        economic_policy=EconomicProjectionPolicy(
            policy_id="public.t02.economic.constraints",
            version="1",
            maximum_absolute_basis_fraction=Decimal("0.5"),
            maximum_volatility_curvature=Decimal("0.25"),
        ),
        stress_return=Decimal("-0.1"),
        stress_basis_shift=entry_price * Decimal("0.01"),
        liquidity_haircut=Decimal("0.2"),
        claimed_selected_contract_id=selected_contract_id,
        claimed_contract_multiplier=multiplier,
    )


__all__ = [
    "build_public_t01_inputs",
    "build_public_t02_inputs",
    "EmpiricalCostCapacitySource",
    "FuturesMarginSource",
    "InputFixtureSemantics",
    "IssuerIdentifierQuery",
    "PUBLIC_SPOT_FUTURES_PROFILE_SCHEMA_VERSION",
    "PUBLIC_T01_PROFILE_ID",
    "PUBLIC_T02_PROFILE_ID",
    "ProviderConventionSource",
    "PublicFuturesProfileInputs",
    "PublicFuturesProfileReceipt",
    "PublicProfileError",
    "PublicSpotFuturesConformanceService",
    "PublicSpotProfileInputs",
    "PublicSpotProfileReceipt",
    "run_public_t01_spot_profile",
    "run_public_t02_futures_profile",
]
