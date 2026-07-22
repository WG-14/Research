"""Point-in-time option cleaning, selection, path marking, and attribution.

The established derivative engine remains authoritative for fills, valuation,
and lifecycle settlement.  This module closes the research path around it: raw
surface observations and exclusions are retained, a concrete listed contract
is selected from data known at the decision instant, and every intermediate
mark is reconciled through a Greek P&L attribution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, Sequence

from market_research.research.hashing import sha256_prefixed


ZERO = Decimal("0")
ONE = Decimal("1")


class OptionPathError(ValueError):
    """Raised when option path evidence is incomplete or temporally unsafe."""


class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class DeltaFallback(str, Enum):
    REJECT = "REJECT"
    NEAREST_WITH_EVIDENCE = "NEAREST_WITH_EVIDENCE"


class ForwardMethod(str, Enum):
    SPOT_CARRY = "SPOT_CARRY"
    FUTURES_PRICE = "FUTURES_PRICE"
    PUT_CALL_PARITY = "PUT_CALL_PARITY"
    BORROW_ADJUSTED_CARRY = "BORROW_ADJUSTED_CARRY"


def _text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == ZERO else format(normalized, "f")


def _utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise OptionPathError(f"{field_name} must be timezone-aware UTC")


def _nonempty(value: str, field_name: str) -> None:
    if not value or value.strip() != value:
        raise OptionPathError(f"{field_name} must be non-empty and trimmed")


def _hash_payload(value: object) -> object:
    if isinstance(value, Decimal):
        return _text(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _hash_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_hash_payload(item) for item in value]
    return value


def _hash(value: object, *, label: str) -> str:
    return sha256_prefixed(_hash_payload(value), label=label)


@dataclass(frozen=True, slots=True)
class PricingModelSpecification:
    model_id: str
    implementation_version: str
    exercise_styles: tuple[str, ...]
    day_count: str
    rate_curve_id: str
    dividend_model: str
    discrete_dividend_policy: str
    borrow_policy: str
    numerical_method: str
    convergence_policy: str

    def __post_init__(self) -> None:
        for field_name in (
            "model_id",
            "implementation_version",
            "day_count",
            "rate_curve_id",
            "dividend_model",
            "discrete_dividend_policy",
            "borrow_policy",
            "numerical_method",
            "convergence_policy",
        ):
            _nonempty(str(getattr(self, field_name)), field_name)
        if not self.exercise_styles:
            raise OptionPathError("exercise_styles cannot be empty")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-pricing-model-specification")


class CommonOptionPricingModel(Protocol):
    """Common model seam; assumptions live in a separate specification."""

    @property
    def specification(self) -> PricingModelSpecification: ...

    def value(self, contract: object, market_state: object) -> Decimal: ...

    def greeks(self, contract: object, market_state: object) -> "OptionGreeks": ...

    def implied_parameter(
        self,
        contract: object,
        observed_price: Decimal,
        market_state: object,
    ) -> Decimal: ...

    def scenario_value(
        self,
        contract: object,
        shocked_market_state: object,
    ) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class OptionGreeks:
    """Per-underlying-unit Greeks with explicit theta/rho conventions."""

    delta: Decimal
    gamma: Decimal
    vega_per_vol_point: Decimal
    theta_per_calendar_day: Decimal
    rho_per_rate_point: Decimal
    vanna: Decimal | None = None
    volga: Decimal | None = None
    charm: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ForwardEstimate:
    value: Decimal
    method: ForwardMethod
    estimated_at: datetime
    input_hashes: tuple[str, ...]
    rate: Decimal
    dividend_yield: Decimal
    borrow_rate: Decimal

    def __post_init__(self) -> None:
        if self.value <= ZERO:
            raise OptionPathError("forward value must be positive")
        _utc(self.estimated_at, "estimated_at")
        if not self.input_hashes:
            raise OptionPathError("forward estimate requires input lineage")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-forward-estimate")


@dataclass(frozen=True, slots=True)
class RawOptionObservation:
    contract_id: str
    underlying_id: str
    right: OptionRight
    strike: Decimal
    expiry: datetime
    observed_at: datetime
    known_at: datetime
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    volume: int
    open_interest: int
    bid_iv: Decimal | None
    ask_iv: Decimal | None
    delta: Decimal | None
    source_quote_hash: str
    adjusted_contract: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.contract_id, "contract_id")
        _nonempty(self.underlying_id, "underlying_id")
        _nonempty(self.source_quote_hash, "source_quote_hash")
        _utc(self.expiry, "expiry")
        _utc(self.observed_at, "observed_at")
        _utc(self.known_at, "known_at")
        if self.known_at < self.observed_at:
            raise OptionPathError("known_at cannot precede observed_at")
        if self.expiry <= self.observed_at or self.strike <= ZERO:
            raise OptionPathError("observation must precede a positive-strike expiry")
        if self.bid_size < ZERO or self.ask_size < ZERO:
            raise OptionPathError("quote sizes cannot be negative")
        if self.volume < 0 or self.open_interest < 0:
            raise OptionPathError("liquidity counts cannot be negative")


@dataclass(frozen=True, slots=True)
class OptionCleaningPolicy:
    policy_id: str
    version: str
    maximum_age_seconds: int
    maximum_relative_spread: Decimal
    minimum_quote_size: Decimal
    minimum_volume: int
    minimum_open_interest: int
    minimum_iv: Decimal
    maximum_iv: Decimal
    reject_adjusted_contracts: bool

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        _nonempty(self.version, "version")
        if self.maximum_age_seconds < 0:
            raise OptionPathError("maximum_age_seconds cannot be negative")
        if self.maximum_relative_spread <= ZERO:
            raise OptionPathError("maximum_relative_spread must be positive")
        if self.minimum_quote_size < ZERO:
            raise OptionPathError("minimum_quote_size cannot be negative")
        if self.minimum_volume < 0 or self.minimum_open_interest < 0:
            raise OptionPathError("liquidity thresholds cannot be negative")
        if self.minimum_iv <= ZERO or self.maximum_iv <= self.minimum_iv:
            raise OptionPathError("IV bounds are invalid")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-cleaning-policy")


@dataclass(frozen=True, slots=True)
class SurfaceRawPoint:
    """Raw and cleaned values remain queryable even when a point is excluded."""

    contract_id: str
    expiry: datetime
    strike: Decimal
    right: OptionRight
    raw_bid: Decimal | None
    raw_ask: Decimal | None
    raw_bid_iv: Decimal | None
    raw_ask_iv: Decimal | None
    cleaned_iv: Decimal | None
    delta: Decimal | None
    spot_moneyness: Decimal
    forward_moneyness: Decimal
    log_moneyness: Decimal
    total_variance: Decimal | None
    liquidity_weight: Decimal
    exclusion_reasons: tuple[str, ...]
    quote_hash: str
    forward_hash: str
    cleaning_policy_hash: str
    known_at: datetime

    @property
    def included(self) -> bool:
        return not self.exclusion_reasons

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-surface-raw-point")


@dataclass(frozen=True, slots=True)
class CleanedOptionChain:
    underlying_id: str
    decision_at: datetime
    points: tuple[SurfaceRawPoint, ...]
    forward: ForwardEstimate
    policy_hash: str
    source_quote_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _nonempty(self.underlying_id, "underlying_id")
        _utc(self.decision_at, "decision_at")
        if not self.points:
            raise OptionPathError("cleaned chain cannot be empty")
        if any(point.known_at > self.decision_at for point in self.points):
            raise OptionPathError("cleaned chain contains future knowledge")
        if len(self.source_quote_hashes) != len(set(self.source_quote_hashes)):
            raise OptionPathError("source quote hashes must be unique")
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                {
                    "underlying_id": self.underlying_id,
                    "decision_at": self.decision_at,
                    "points": [point.content_hash for point in self.points],
                    "forward_hash": self.forward.content_hash,
                    "policy_hash": self.policy_hash,
                    "source_quote_hashes": self.source_quote_hashes,
                },
                label="cleaned-option-chain",
            ),
        )

    @property
    def included_points(self) -> tuple[SurfaceRawPoint, ...]:
        return tuple(point for point in self.points if point.included)


class OptionChainCleaner:
    def __init__(self, policy: OptionCleaningPolicy) -> None:
        self._policy = policy

    def clean(
        self,
        *,
        underlying_id: str,
        decision_at: datetime,
        spot: Decimal,
        forward: ForwardEstimate,
        observations: Sequence[RawOptionObservation],
    ) -> CleanedOptionChain:
        _utc(decision_at, "decision_at")
        if spot <= ZERO:
            raise OptionPathError("spot must be positive")
        if forward.estimated_at > decision_at:
            raise OptionPathError("forward estimate is future knowledge")
        if not observations:
            raise OptionPathError("raw option observations are required")
        points = tuple(
            self._clean_one(
                item,
                underlying_id=underlying_id,
                decision_at=decision_at,
                spot=spot,
                forward=forward,
            )
            for item in sorted(observations, key=lambda item: item.contract_id)
        )
        return CleanedOptionChain(
            underlying_id=underlying_id,
            decision_at=decision_at,
            points=points,
            forward=forward,
            policy_hash=self._policy.content_hash,
            source_quote_hashes=tuple(
                sorted({item.source_quote_hash for item in observations})
            ),
        )

    def _clean_one(
        self,
        item: RawOptionObservation,
        *,
        underlying_id: str,
        decision_at: datetime,
        spot: Decimal,
        forward: ForwardEstimate,
    ) -> SurfaceRawPoint:
        reasons: list[str] = []
        if item.underlying_id != underlying_id:
            reasons.append("UNDERLYING_MISMATCH")
        if item.known_at > decision_at:
            reasons.append("FUTURE_KNOWLEDGE")
        age = (decision_at - item.observed_at).total_seconds()
        if age > self._policy.maximum_age_seconds:
            reasons.append("STALE_QUOTE")
        if item.adjusted_contract and self._policy.reject_adjusted_contracts:
            reasons.append("ADJUSTED_CONTRACT")
        if item.bid is None or item.ask is None:
            reasons.append("MISSING_TWO_SIDED_QUOTE")
        elif item.bid <= ZERO or item.ask <= ZERO or item.bid > item.ask:
            reasons.append("INVALID_QUOTE")
        else:
            midpoint = (item.bid + item.ask) / Decimal("2")
            if (item.ask - item.bid) / midpoint > self._policy.maximum_relative_spread:
                reasons.append("SPREAD_TOO_WIDE")
        if min(item.bid_size, item.ask_size) < self._policy.minimum_quote_size:
            reasons.append("INSUFFICIENT_QUOTE_SIZE")
        if item.volume < self._policy.minimum_volume:
            reasons.append("INSUFFICIENT_VOLUME")
        if item.open_interest < self._policy.minimum_open_interest:
            reasons.append("INSUFFICIENT_OPEN_INTEREST")
        if item.bid_iv is None or item.ask_iv is None:
            reasons.append("IV_INVERSION_FAILED")
        elif not (
            self._policy.minimum_iv
            <= item.bid_iv
            <= item.ask_iv
            <= self._policy.maximum_iv
        ):
            reasons.append("IV_RANGE_INVALID")
        if item.delta is None:
            reasons.append("GREEKS_UNAVAILABLE")

        liquidity = min(
            ONE,
            min(item.bid_size, item.ask_size)
            / max(self._policy.minimum_quote_size, ONE)
            * Decimal("0.25")
            + Decimal(item.volume)
            / Decimal(max(self._policy.minimum_volume, 1))
            * Decimal("0.25")
            + Decimal(item.open_interest)
            / Decimal(max(self._policy.minimum_open_interest, 1))
            * Decimal("0.25"),
        )
        cleaned_iv = (
            (item.bid_iv + item.ask_iv) / Decimal("2")
            if not reasons and item.bid_iv is not None and item.ask_iv is not None
            else None
        )
        years = Decimal(str((item.expiry - decision_at).total_seconds())) / Decimal(
            "31557600"
        )
        spot_moneyness = item.strike / spot
        forward_moneyness = item.strike / forward.value
        # Decimal has a deterministic natural logarithm in supported Python.
        log_moneyness = forward_moneyness.ln()
        total_variance = cleaned_iv * cleaned_iv * years if cleaned_iv else None
        return SurfaceRawPoint(
            contract_id=item.contract_id,
            expiry=item.expiry,
            strike=item.strike,
            right=item.right,
            raw_bid=item.bid,
            raw_ask=item.ask,
            raw_bid_iv=item.bid_iv,
            raw_ask_iv=item.ask_iv,
            cleaned_iv=cleaned_iv,
            delta=item.delta,
            spot_moneyness=spot_moneyness,
            forward_moneyness=forward_moneyness,
            log_moneyness=log_moneyness,
            total_variance=total_variance,
            liquidity_weight=liquidity,
            exclusion_reasons=tuple(reasons),
            quote_hash=item.source_quote_hash,
            forward_hash=forward.content_hash,
            cleaning_policy_hash=self._policy.content_hash,
            known_at=item.known_at,
        )


@dataclass(frozen=True, slots=True)
class OptionSelectionPolicy:
    policy_id: str
    version: str
    right: OptionRight
    target_days_to_expiry: int
    minimum_days_to_expiry: int
    maximum_days_to_expiry: int
    target_delta: Decimal
    maximum_delta_distance: Decimal
    minimum_liquidity_weight: Decimal
    fallback: DeltaFallback

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        _nonempty(self.version, "version")
        if not (
            0
            <= self.minimum_days_to_expiry
            <= self.target_days_to_expiry
            <= self.maximum_days_to_expiry
        ):
            raise OptionPathError("days-to-expiry policy is inconsistent")
        if self.maximum_delta_distance < ZERO:
            raise OptionPathError("maximum_delta_distance cannot be negative")
        if not ZERO <= self.minimum_liquidity_weight <= ONE:
            raise OptionPathError("minimum_liquidity_weight must be in [0, 1]")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-selection-policy")


@dataclass(frozen=True, slots=True)
class CalculatedOptionDelta:
    """Decision-time delta calculated by a versioned pricing model.

    Raw chain deltas remain available as immutable supplier observations, but
    contract selection consumes this separate derived record.  The timestamps
    and hashes make it impossible to silently substitute a future Greek or an
    unbound vendor field for a decision-time model result.
    """

    contract_id: str
    calculated_at: datetime
    known_at: datetime
    delta: Decimal
    market_state_hash: str
    model_specification_hash: str
    valuation_input_hash: str

    def __post_init__(self) -> None:
        _nonempty(self.contract_id, "calculated_delta.contract_id")
        _utc(self.calculated_at, "calculated_delta.calculated_at")
        _utc(self.known_at, "calculated_delta.known_at")
        if self.known_at < self.calculated_at:
            raise OptionPathError("calculated delta known_at precedes calculated_at")
        if (
            isinstance(self.delta, bool)
            or not isinstance(self.delta, Decimal)
            or not self.delta.is_finite()
            or not -ONE <= self.delta <= ONE
        ):
            raise OptionPathError("calculated delta must be finite and in [-1, 1]")
        for field_name in (
            "market_state_hash",
            "model_specification_hash",
            "valuation_input_hash",
        ):
            _nonempty(str(getattr(self, field_name)), f"calculated_delta.{field_name}")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="calculated-option-delta")


@dataclass(frozen=True, slots=True)
class OptionSelectionDecision:
    decision_at: datetime
    chain_hash: str
    policy_hash: str
    eligible_contract_ids: tuple[str, ...]
    selected_contract_id: str | None
    selected_expiry: datetime | None
    selected_strike: Decimal | None
    selected_delta: Decimal | None
    selected_delta_evidence_hash: str | None
    delta_distance: Decimal | None
    exact_tolerance_match: bool
    rejection_reason: str | None

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-selection-decision")


def select_option_contract(
    chain: CleanedOptionChain,
    policy: OptionSelectionPolicy,
    calculated_deltas: Sequence[CalculatedOptionDelta],
) -> OptionSelectionDecision:
    ordered_deltas = tuple(sorted(calculated_deltas, key=lambda item: item.contract_id))
    if not ordered_deltas:
        raise OptionPathError("calculated option deltas are required for selection")
    if len({item.contract_id for item in ordered_deltas}) != len(ordered_deltas):
        raise OptionPathError("calculated option deltas contain duplicate contract ids")
    if any(
        item.calculated_at > chain.decision_at or item.known_at > chain.decision_at
        for item in ordered_deltas
    ):
        raise OptionPathError("calculated option delta contains future knowledge")
    delta_by_contract = {item.contract_id: item for item in ordered_deltas}
    unknown_contracts = set(delta_by_contract) - {
        point.contract_id for point in chain.points
    }
    if unknown_contracts:
        raise OptionPathError(
            "calculated option delta references unknown contract:"
            + ",".join(sorted(unknown_contracts))
        )

    candidates: list[tuple[SurfaceRawPoint, CalculatedOptionDelta]] = []
    for point in chain.included_points:
        days = int((point.expiry - chain.decision_at).total_seconds() // 86400)
        calculated_delta = delta_by_contract.get(point.contract_id)
        if (
            point.right is policy.right
            and policy.minimum_days_to_expiry <= days <= policy.maximum_days_to_expiry
            and point.liquidity_weight >= policy.minimum_liquidity_weight
            and calculated_delta is not None
        ):
            candidates.append((point, calculated_delta))
    candidates.sort(
        key=lambda candidate: (
            abs(
                int((candidate[0].expiry - chain.decision_at).total_seconds() // 86400)
                - policy.target_days_to_expiry
            ),
            abs(candidate[1].delta - policy.target_delta),
            -candidate[0].liquidity_weight,
            candidate[0].contract_id,
        )
    )
    if not candidates:
        return OptionSelectionDecision(
            decision_at=chain.decision_at,
            chain_hash=chain.content_hash,
            policy_hash=policy.content_hash,
            eligible_contract_ids=(),
            selected_contract_id=None,
            selected_expiry=None,
            selected_strike=None,
            selected_delta=None,
            selected_delta_evidence_hash=None,
            delta_distance=None,
            exact_tolerance_match=False,
            rejection_reason="NO_ELIGIBLE_LISTED_CONTRACT",
        )
    selected, selected_delta = candidates[0]
    distance = abs(selected_delta.delta - policy.target_delta)
    within = distance <= policy.maximum_delta_distance
    if not within and policy.fallback is DeltaFallback.REJECT:
        return OptionSelectionDecision(
            decision_at=chain.decision_at,
            chain_hash=chain.content_hash,
            policy_hash=policy.content_hash,
            eligible_contract_ids=tuple(item.contract_id for item, _ in candidates),
            selected_contract_id=None,
            selected_expiry=None,
            selected_strike=None,
            selected_delta=None,
            selected_delta_evidence_hash=None,
            delta_distance=distance,
            exact_tolerance_match=False,
            rejection_reason="NO_CONTRACT_WITHIN_DELTA_TOLERANCE",
        )
    return OptionSelectionDecision(
        decision_at=chain.decision_at,
        chain_hash=chain.content_hash,
        policy_hash=policy.content_hash,
        eligible_contract_ids=tuple(item.contract_id for item, _ in candidates),
        selected_contract_id=selected.contract_id,
        selected_expiry=selected.expiry,
        selected_strike=selected.strike,
        selected_delta=selected_delta.delta,
        selected_delta_evidence_hash=selected_delta.content_hash,
        delta_distance=distance,
        exact_tolerance_match=within,
        rejection_reason=None if within else "NEAREST_CONTRACT_FALLBACK",
    )


@dataclass(frozen=True, slots=True)
class OptionPathMark:
    contract_id: str
    marked_at: datetime
    market_state_hash: str
    market_quote_hash: str
    model_specification_hash: str
    market_price: Decimal
    theoretical_price: Decimal
    spot_price: Decimal
    implied_volatility: Decimal
    rate: Decimal
    dividend_yield: Decimal
    skew: Decimal
    greeks: OptionGreeks
    hedge_pnl_since_previous: Decimal = ZERO
    carry_pnl_since_previous: Decimal = ZERO
    slippage_since_previous: Decimal = ZERO
    transaction_cost_since_previous: Decimal = ZERO

    def __post_init__(self) -> None:
        _nonempty(self.contract_id, "contract_id")
        _utc(self.marked_at, "marked_at")
        for field_name in (
            "market_state_hash",
            "market_quote_hash",
            "model_specification_hash",
        ):
            _nonempty(str(getattr(self, field_name)), field_name)
        if (
            min(
                self.market_price,
                self.theoretical_price,
                self.spot_price,
                self.implied_volatility,
                self.slippage_since_previous,
                self.transaction_cost_since_previous,
            )
            < ZERO
        ):
            raise OptionPathError("prices, IV, slippage, and costs cannot be negative")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-path-mark")


@dataclass(frozen=True, slots=True)
class OptionAttributionInterval:
    start_at: datetime
    end_at: datetime
    delta_pnl: Decimal
    gamma_pnl: Decimal
    vega_pnl: Decimal
    theta_pnl: Decimal
    carry_pnl: Decimal
    hedge_pnl: Decimal
    slippage_pnl: Decimal
    transaction_cost_pnl: Decimal
    model_residual: Decimal
    actual_pnl: Decimal

    @property
    def attributed_pnl(self) -> Decimal:
        return (
            self.delta_pnl
            + self.gamma_pnl
            + self.vega_pnl
            + self.theta_pnl
            + self.carry_pnl
            + self.hedge_pnl
            + self.slippage_pnl
            + self.transaction_cost_pnl
            + self.model_residual
        )


@dataclass(frozen=True, slots=True)
class OptionAttributionPolicy:
    policy_id: str
    version: str
    maximum_absolute_residual: Decimal
    maximum_relative_residual: Decimal

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "option_attribution_policy.policy_id")
        _nonempty(self.version, "option_attribution_policy.version")
        for field_name in (
            "maximum_absolute_residual",
            "maximum_relative_residual",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, Decimal)
                or not value.is_finite()
                or value < ZERO
            ):
                raise OptionPathError(
                    f"option_attribution_policy.{field_name} must be a "
                    "non-negative finite Decimal"
                )
        if self.maximum_relative_residual > ONE:
            raise OptionPathError(
                "option_attribution_policy.maximum_relative_residual exceeds one"
            )

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="option-attribution-policy")

    def permits(self, *, residual: Decimal, option_price_pnl: Decimal) -> bool:
        """Evaluate model error against the option leg, not auxiliary cash flows.

        Hedge, carry, slippage, and transaction-cost cash flows reconcile in the
        interval arithmetic, but they must not enlarge the relative model-error
        allowance.  Otherwise a large unrelated hedge cash flow could make an
        arbitrarily poor Greek explanation appear acceptable.
        """

        relative_limit = abs(option_price_pnl) * self.maximum_relative_residual
        allowed = max(self.maximum_absolute_residual, relative_limit)
        return abs(residual) <= allowed


@dataclass(frozen=True, slots=True)
class OptionPathAttribution:
    contract_id: str
    position_quantity: Decimal
    multiplier: Decimal
    intervals: tuple[OptionAttributionInterval, ...]
    mark_hashes: tuple[str, ...]
    model_specification_hash: str
    policy_hash: str
    total_model_residual: Decimal
    maximum_observed_absolute_residual: Decimal
    actual_pnl: Decimal
    attributed_pnl: Decimal
    reconciled: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.intervals:
            raise OptionPathError("attribution requires an intermediate path")
        for field_name in ("model_specification_hash", "policy_hash"):
            _nonempty(str(getattr(self, field_name)), field_name)
        if self.total_model_residual != sum(
            (item.model_residual for item in self.intervals), ZERO
        ):
            raise OptionPathError("option attribution residual total mismatch")
        if self.maximum_observed_absolute_residual != max(
            abs(item.model_residual) for item in self.intervals
        ):
            raise OptionPathError("option attribution maximum residual mismatch")
        if self.actual_pnl != self.attributed_pnl or not self.reconciled:
            raise OptionPathError("option attribution does not reconcile")
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                {
                    "contract_id": self.contract_id,
                    "position_quantity": self.position_quantity,
                    "multiplier": self.multiplier,
                    "intervals": [asdict(item) for item in self.intervals],
                    "mark_hashes": self.mark_hashes,
                    "model_specification_hash": self.model_specification_hash,
                    "policy_hash": self.policy_hash,
                    "total_model_residual": self.total_model_residual,
                    "maximum_observed_absolute_residual": (
                        self.maximum_observed_absolute_residual
                    ),
                    "actual_pnl": self.actual_pnl,
                },
                label="option-path-attribution",
            ),
        )


def attribute_option_path(
    marks: Sequence[OptionPathMark],
    *,
    signed_quantity: Decimal,
    multiplier: Decimal,
    policy: OptionAttributionPolicy,
) -> OptionPathAttribution:
    if not isinstance(policy, OptionAttributionPolicy):
        raise OptionPathError("option attribution policy is required")
    if len(marks) < 2:
        raise OptionPathError("at least entry and one later mark are required")
    if signed_quantity == ZERO or multiplier <= ZERO:
        raise OptionPathError(
            "position quantity and multiplier must be non-zero/positive"
        )
    ordered = tuple(marks)
    contract_ids = {mark.contract_id for mark in ordered}
    if len(contract_ids) != 1:
        raise OptionPathError("path marks must reference one actual contract")
    if any(
        later.marked_at <= earlier.marked_at
        for earlier, later in zip(ordered, ordered[1:])
    ):
        raise OptionPathError("path marks must be strictly chronological")
    model_specification_hashes = {mark.model_specification_hash for mark in ordered}
    if len(model_specification_hashes) != 1:
        raise OptionPathError("path marks changed pricing model specification")
    if len({mark.content_hash for mark in ordered}) != len(ordered):
        raise OptionPathError("path marks contain duplicate evidence")
    if len({mark.market_state_hash for mark in ordered}) != len(ordered):
        raise OptionPathError("path marks contain duplicate market states")
    if len({mark.market_quote_hash for mark in ordered}) != len(ordered):
        raise OptionPathError("path marks contain duplicate market quotes")

    scale = signed_quantity * multiplier
    intervals: list[OptionAttributionInterval] = []
    for previous, current in zip(ordered, ordered[1:]):
        spot_change = current.spot_price - previous.spot_price
        vol_change_points = (
            current.implied_volatility - previous.implied_volatility
        ) * Decimal("100")
        elapsed_days = Decimal(
            str((current.marked_at - previous.marked_at).total_seconds())
        ) / Decimal("86400")
        delta_pnl = scale * previous.greeks.delta * spot_change
        gamma_pnl = (
            scale * Decimal("0.5") * previous.greeks.gamma * spot_change * spot_change
        )
        vega_pnl = scale * previous.greeks.vega_per_vol_point * vol_change_points
        theta_pnl = scale * previous.greeks.theta_per_calendar_day * elapsed_days
        carry_pnl = current.carry_pnl_since_previous
        hedge_pnl = current.hedge_pnl_since_previous
        slippage_pnl = -current.slippage_since_previous
        transaction_cost_pnl = -current.transaction_cost_since_previous
        option_price_pnl = scale * (current.market_price - previous.market_price)
        actual = (
            option_price_pnl
            + carry_pnl
            + hedge_pnl
            + slippage_pnl
            + transaction_cost_pnl
        )
        explained = (
            delta_pnl
            + gamma_pnl
            + vega_pnl
            + theta_pnl
            + carry_pnl
            + hedge_pnl
            + slippage_pnl
            + transaction_cost_pnl
        )
        residual = actual - explained
        if not policy.permits(
            residual=residual,
            option_price_pnl=option_price_pnl,
        ):
            raise OptionPathError("option attribution model residual exceeds policy")
        interval = OptionAttributionInterval(
            start_at=previous.marked_at,
            end_at=current.marked_at,
            delta_pnl=delta_pnl,
            gamma_pnl=gamma_pnl,
            vega_pnl=vega_pnl,
            theta_pnl=theta_pnl,
            carry_pnl=carry_pnl,
            hedge_pnl=hedge_pnl,
            slippage_pnl=slippage_pnl,
            transaction_cost_pnl=transaction_cost_pnl,
            model_residual=residual,
            actual_pnl=actual,
        )
        if interval.attributed_pnl != interval.actual_pnl:
            raise OptionPathError("interval attribution arithmetic failed")
        intervals.append(interval)
    actual_total = sum((item.actual_pnl for item in intervals), ZERO)
    attributed_total = sum((item.attributed_pnl for item in intervals), ZERO)
    total_residual = sum((item.model_residual for item in intervals), ZERO)
    return OptionPathAttribution(
        contract_id=ordered[0].contract_id,
        position_quantity=signed_quantity,
        multiplier=multiplier,
        intervals=tuple(intervals),
        mark_hashes=tuple(item.content_hash for item in ordered),
        model_specification_hash=ordered[0].model_specification_hash,
        policy_hash=policy.content_hash,
        total_model_residual=total_residual,
        maximum_observed_absolute_residual=max(
            abs(item.model_residual) for item in intervals
        ),
        actual_pnl=actual_total,
        attributed_pnl=attributed_total,
        reconciled=actual_total == attributed_total,
    )


DEFAULT_OPTION_CLEANING_POLICY = OptionCleaningPolicy(
    policy_id="option-cleaning-v1",
    version="1.0.0",
    maximum_age_seconds=120,
    maximum_relative_spread=Decimal("0.30"),
    minimum_quote_size=Decimal("1"),
    minimum_volume=1,
    minimum_open_interest=1,
    minimum_iv=Decimal("0.0001"),
    maximum_iv=Decimal("5"),
    reject_adjusted_contracts=True,
)


__all__ = [
    "CleanedOptionChain",
    "CommonOptionPricingModel",
    "DEFAULT_OPTION_CLEANING_POLICY",
    "DeltaFallback",
    "ForwardEstimate",
    "ForwardMethod",
    "OptionAttributionInterval",
    "OptionChainCleaner",
    "OptionCleaningPolicy",
    "OptionGreeks",
    "OptionPathAttribution",
    "OptionPathError",
    "OptionPathMark",
    "OptionRight",
    "OptionSelectionDecision",
    "OptionSelectionPolicy",
    "PricingModelSpecification",
    "RawOptionObservation",
    "SurfaceRawPoint",
    "attribute_option_path",
    "select_option_contract",
]
