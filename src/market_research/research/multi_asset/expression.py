"""Hypothesis-to-instrument expression for offline multi-asset research.

The contracts in this module deliberately separate five decisions which are
often (and incorrectly) collapsed into a ticker-level signal::

    economic hypothesis -> desired payoff -> candidate expressions
    -> concrete point-in-time instrument selection -> sized positions

The engine does not price derivatives.  Product engines provide conservative
candidate estimates and this module records, validates, compares, and selects
them under one deterministic policy.  No class in this module can submit an
order or access a network/account system.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
from itertools import product
from typing import Iterable, Mapping, Sequence

from market_research.research.hashing import sha256_prefixed


ZERO = Decimal("0")
ONE = Decimal("1")


class ExpressionValidationError(ValueError):
    """Raised when an expression would be ambiguous or non-reproducible."""


class ProductKind(str, Enum):
    SPOT = "SPOT"
    ETF = "ETF"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> Decimal:
        return ONE if self is Direction.LONG else Decimal("-1")


class LegRole(str, Enum):
    PRIMARY = "PRIMARY"
    HEDGE = "HEDGE"
    INCOME = "INCOME"
    FINANCING = "FINANCING"
    TAIL_PROTECTION = "TAIL_PROTECTION"


class LegState(str, Enum):
    PLANNED = "PLANNED"
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    EXERCISED = "EXERCISED"
    ASSIGNED = "ASSIGNED"


class ExecutionMode(str, Enum):
    SIMULTANEOUS_ATOMIC = "SIMULTANEOUS_ATOMIC"
    SEQUENTIAL = "SEQUENTIAL"
    COMPLEX_MID = "COMPLEX_MID"
    COMPLEX_CONSERVATIVE = "COMPLEX_CONSERVATIVE"


class ExpressionKind(str, Enum):
    SPOT = "SPOT"
    ETF = "ETF"
    FUTURE = "FUTURE"
    CALL_OR_PUT = "CALL_OR_PUT"
    OPTION_SPREAD = "OPTION_SPREAD"
    SPOT_OPTION = "SPOT_OPTION"
    FUTURE_OPTION = "FUTURE_OPTION"
    MULTI_LEG = "MULTI_LEG"


def _require_text(value: str, field: str) -> None:
    if not value or value.strip() != value:
        raise ExpressionValidationError(f"{field} must be non-empty and trimmed")


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ExpressionValidationError(f"{field} must be timezone-aware UTC")


def _require_fraction(value: Decimal, field: str) -> None:
    if value < ZERO or value > ONE:
        raise ExpressionValidationError(f"{field} must be in [0, 1]")


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return "0" if normalized == ZERO else format(normalized, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _content_hash(value: object, *, label: str) -> str:
    return sha256_prefixed(_canonical_value(value), label=label)


@dataclass(frozen=True, slots=True)
class ScenarioRange:
    """A probability-weighted, explicitly bounded market outcome."""

    name: str
    probability: Decimal
    lower_return: Decimal
    upper_return: Decimal

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_fraction(self.probability, "probability")
        if self.lower_return > self.upper_return:
            raise ExpressionValidationError(
                "scenario lower_return exceeds upper_return"
            )


@dataclass(frozen=True, slots=True)
class ExpectedMarketDistribution:
    """An economic forecast independent of any implementation instrument."""

    expected_return: Decimal
    annualized_volatility: Decimal
    downside_tail_return: Decimal
    upside_return: Decimal
    horizon_days: int
    risk_free_rate: Decimal
    dividend_yield: Decimal
    volatility_change: Decimal
    liquidity_change: Decimal
    scenarios: tuple[ScenarioRange, ...]

    def __post_init__(self) -> None:
        if self.horizon_days <= 0:
            raise ExpressionValidationError("horizon_days must be positive")
        if self.annualized_volatility < ZERO:
            raise ExpressionValidationError("annualized_volatility cannot be negative")
        if self.downside_tail_return > self.expected_return:
            raise ExpressionValidationError(
                "downside tail must not exceed expected return"
            )
        if self.upside_return < self.expected_return:
            raise ExpressionValidationError("upside must not be below expected return")
        if not self.scenarios:
            raise ExpressionValidationError("at least one scenario is required")
        probability = sum((item.probability for item in self.scenarios), ZERO)
        if abs(probability - ONE) > Decimal("0.00000001"):
            raise ExpressionValidationError("scenario probabilities must sum to one")
        names = [item.name for item in self.scenarios]
        if len(names) != len(set(names)):
            raise ExpressionValidationError("scenario names must be unique")


@dataclass(frozen=True, slots=True)
class EconomicHypothesis:
    """Versioned thesis that intentionally contains no ticker or contract ID."""

    hypothesis_id: str
    version: str
    economic_underlying_id: str
    rationale: str
    expected_direction: Direction
    distribution: ExpectedMarketDistribution
    conditions: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    prediction_target: str
    evaluation_metrics: tuple[str, ...]
    data_limitations: tuple[str, ...] = ()
    model_risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "hypothesis_id",
            "version",
            "economic_underlying_id",
            "rationale",
            "prediction_target",
        ):
            _require_text(str(getattr(self, field)), field)
        if not self.conditions or not self.failure_conditions:
            raise ExpressionValidationError(
                "hypothesis requires both validity and falsification conditions"
            )
        if not self.evaluation_metrics:
            raise ExpressionValidationError("evaluation_metrics cannot be empty")

    @property
    def content_hash(self) -> str:
        return _content_hash(asdict(self), label="economic-hypothesis")


@dataclass(frozen=True, slots=True)
class DesiredEconomicPayoff:
    """Product-neutral payoff and risk objective derived from a hypothesis."""

    underlying_id: str
    direction: Direction
    horizon_days: int
    target_notional: Decimal
    target_delta: Decimal | None
    target_vega: Decimal | None
    target_volatility: Decimal | None
    maximum_loss: Decimal
    maximum_premium: Decimal | None
    tail_protection_required: bool
    bounded_loss_required: bool
    allowed_expression_kinds: tuple[ExpressionKind, ...]

    def __post_init__(self) -> None:
        _require_text(self.underlying_id, "underlying_id")
        if self.horizon_days <= 0 or self.target_notional <= ZERO:
            raise ExpressionValidationError(
                "horizon_days and target_notional must be positive"
            )
        if self.maximum_loss <= ZERO:
            raise ExpressionValidationError("maximum_loss must be positive")
        if self.maximum_premium is not None and self.maximum_premium <= ZERO:
            raise ExpressionValidationError("maximum_premium must be positive")
        if not self.allowed_expression_kinds:
            raise ExpressionValidationError("allowed_expression_kinds cannot be empty")
        if len(set(self.allowed_expression_kinds)) != len(
            self.allowed_expression_kinds
        ):
            raise ExpressionValidationError("allowed expression kinds must be unique")


@dataclass(frozen=True, slots=True)
class LegSelectionRule:
    """Auditable contract/listing selection and sizing constraints for one leg."""

    product_kind: ProductKind
    minimum_days_to_expiry: int | None = None
    maximum_days_to_expiry: int | None = None
    target_delta: Decimal | None = None
    target_vega: Decimal | None = None
    target_moneyness: Decimal | None = None
    minimum_liquidity_score: Decimal = ZERO
    roll_rule_id: str | None = None
    hedge_underlying_id: str | None = None
    sizing_method: str = "TARGET_NOTIONAL"

    def __post_init__(self) -> None:
        _require_fraction(self.minimum_liquidity_score, "minimum_liquidity_score")
        if self.minimum_days_to_expiry is not None and self.minimum_days_to_expiry < 0:
            raise ExpressionValidationError("minimum_days_to_expiry cannot be negative")
        if self.maximum_days_to_expiry is not None and self.maximum_days_to_expiry < 0:
            raise ExpressionValidationError("maximum_days_to_expiry cannot be negative")
        if (
            self.minimum_days_to_expiry is not None
            and self.maximum_days_to_expiry is not None
            and self.minimum_days_to_expiry > self.maximum_days_to_expiry
        ):
            raise ExpressionValidationError("expiry range is inverted")
        _require_text(self.sizing_method, "sizing_method")


@dataclass(frozen=True, slots=True)
class InstrumentChoice:
    """Concrete product known at a point in time with conservative estimates."""

    instrument_id: str
    economic_underlying_id: str
    product_kind: ProductKind
    currency: str
    known_at: datetime
    unit_price: Decimal
    contract_multiplier: Decimal
    economic_notional_per_unit: Decimal
    liquidity_score: Decimal
    expected_return: Decimal
    expected_carry: Decimal
    expected_roll_cost: Decimal
    expected_time_value_decay: Decimal
    implied_volatility: Decimal | None
    transaction_cost: Decimal
    initial_margin: Decimal
    tail_loss: Decimal
    model_sensitivity: Decimal
    data_confidence: Decimal
    expiry: datetime | None = None
    strike: Decimal | None = None
    delta: Decimal | None = None
    vega: Decimal | None = None
    option_right: str | None = None

    def __post_init__(self) -> None:
        for field in ("instrument_id", "economic_underlying_id", "currency"):
            _require_text(str(getattr(self, field)), field)
        _require_utc(self.known_at, "known_at")
        if self.expiry is not None:
            _require_utc(self.expiry, "expiry")
        if (
            self.unit_price <= ZERO
            or self.contract_multiplier <= ZERO
            or self.economic_notional_per_unit <= ZERO
        ):
            raise ExpressionValidationError(
                "price, multiplier, and economic notional must be positive"
            )
        _require_fraction(self.liquidity_score, "liquidity_score")
        _require_fraction(self.data_confidence, "data_confidence")
        if (
            min(
                self.transaction_cost,
                self.initial_margin,
                self.tail_loss,
                self.model_sensitivity,
            )
            < ZERO
        ):
            raise ExpressionValidationError(
                "cost and risk estimates cannot be negative"
            )
        if self.product_kind is ProductKind.OPTION:
            if self.expiry is None or self.strike is None or self.delta is None:
                raise ExpressionValidationError(
                    "option choice requires expiry, strike, and independently computed delta"
                )
            if self.option_right not in {"CALL", "PUT"}:
                raise ExpressionValidationError("option_right must be CALL or PUT")
        elif self.option_right is not None:
            raise ExpressionValidationError("option_right is valid only for options")

    @property
    def unit_notional(self) -> Decimal:
        return self.economic_notional_per_unit


@dataclass(frozen=True, slots=True)
class ExpressionLeg:
    selection_rule: LegSelectionRule
    instrument_id: str
    direction: Direction
    quantity: Decimal
    ratio: Decimal
    currency: str
    role: LegRole
    entry_state: LegState = LegState.PLANNED
    exit_state: LegState = LegState.PLANNED

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.currency, "currency")
        if self.quantity <= ZERO or self.ratio <= ZERO:
            raise ExpressionValidationError("leg quantity and ratio must be positive")


@dataclass(frozen=True, slots=True)
class StrategyTargets:
    net_delta: Decimal | None = None
    net_vega: Decimal | None = None
    net_gamma: Decimal | None = None
    target_notional: Decimal | None = None
    maximum_premium: Decimal | None = None
    maximum_loss: Decimal | None = None
    collateral_limit: Decimal | None = None
    cash_limit: Decimal | None = None

    def __post_init__(self) -> None:
        for field in (
            "target_notional",
            "maximum_premium",
            "maximum_loss",
            "collateral_limit",
            "cash_limit",
        ):
            value = getattr(self, field)
            if value is not None and value <= ZERO:
                raise ExpressionValidationError(f"{field} must be positive")


@dataclass(frozen=True, slots=True)
class ExpressionCandidate:
    candidate_id: str
    expression_kind: ExpressionKind
    choices: tuple[InstrumentChoice, ...]
    directions: tuple[Direction, ...]
    roles: tuple[LegRole, ...]
    leg_ratios: tuple[Decimal, ...]
    selection_rules: tuple[LegSelectionRule, ...]
    execution_mode: ExecutionMode
    expected_return: Decimal
    pnl_dispersion: Decimal
    maximum_loss: Decimal
    carry: Decimal
    roll_cost: Decimal
    time_value_decay: Decimal
    implied_volatility_cost: Decimal
    liquidity_score: Decimal
    transaction_cost: Decimal
    margin_required: Decimal
    tail_risk: Decimal
    model_sensitivity: Decimal
    data_confidence: Decimal
    targets: StrategyTargets
    legging_risk_limit: Decimal = ZERO
    maximum_leg_time_skew_seconds: int = 0
    allow_partial_fill: bool = False

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        count = len(self.choices)
        if count == 0 or any(
            len(items) != count
            for items in (
                self.directions,
                self.roles,
                self.leg_ratios,
                self.selection_rules,
            )
        ):
            raise ExpressionValidationError(
                "candidate leg arrays must be non-empty/aligned"
            )
        if len({choice.instrument_id for choice in self.choices}) != count:
            raise ExpressionValidationError("candidate instrument IDs must be unique")
        if any(ratio <= ZERO for ratio in self.leg_ratios):
            raise ExpressionValidationError("leg ratios must be positive")
        for field in (
            "pnl_dispersion",
            "maximum_loss",
            "roll_cost",
            "time_value_decay",
            "implied_volatility_cost",
            "transaction_cost",
            "margin_required",
            "tail_risk",
            "model_sensitivity",
            "legging_risk_limit",
        ):
            if getattr(self, field) < ZERO:
                raise ExpressionValidationError(f"{field} cannot be negative")
        _require_fraction(self.liquidity_score, "liquidity_score")
        _require_fraction(self.data_confidence, "data_confidence")
        if self.maximum_leg_time_skew_seconds < 0:
            raise ExpressionValidationError(
                "maximum_leg_time_skew_seconds cannot be negative"
            )
        if count > 1 and self.execution_mode is ExecutionMode.SEQUENTIAL:
            if self.maximum_leg_time_skew_seconds == 0:
                raise ExpressionValidationError(
                    "sequential multi-leg execution requires a positive skew limit"
                )


@dataclass(frozen=True, slots=True)
class ExpressionPolicy:
    """Versioned deterministic feasibility and comparison policy."""

    policy_id: str
    version: str
    minimum_liquidity_score: Decimal
    minimum_data_confidence: Decimal
    maximum_margin_fraction: Decimal
    maximum_transaction_cost_fraction: Decimal
    score_weights: tuple[tuple[str, Decimal], ...]

    def __post_init__(self) -> None:
        _require_text(self.policy_id, "policy_id")
        _require_text(self.version, "version")
        _require_fraction(self.minimum_liquidity_score, "minimum_liquidity_score")
        _require_fraction(self.minimum_data_confidence, "minimum_data_confidence")
        _require_fraction(self.maximum_margin_fraction, "maximum_margin_fraction")
        _require_fraction(
            self.maximum_transaction_cost_fraction,
            "maximum_transaction_cost_fraction",
        )
        keys = [key for key, _ in self.score_weights]
        expected = {
            "expected_return",
            "pnl_dispersion",
            "maximum_loss",
            "carry",
            "roll_cost",
            "time_value_decay",
            "implied_volatility_cost",
            "liquidity_score",
            "transaction_cost",
            "margin_required",
            "tail_risk",
            "model_sensitivity",
            "data_confidence",
        }
        if set(keys) != expected or len(keys) != len(expected):
            raise ExpressionValidationError(
                "score_weights must name every comparison dimension exactly once"
            )

    @property
    def weights(self) -> Mapping[str, Decimal]:
        return dict(self.score_weights)

    @property
    def content_hash(self) -> str:
        return _content_hash(asdict(self), label="expression-policy")


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate_id: str
    feasible: bool
    rejection_reasons: tuple[str, ...]
    comparison_values: tuple[tuple[str, Decimal], ...]
    score: Decimal | None


@dataclass(frozen=True, slots=True)
class ExpressionDecision:
    hypothesis_hash: str
    payoff_hash: str
    policy_hash: str
    as_of: datetime
    candidate_evaluations: tuple[CandidateEvaluation, ...]
    selected_candidate_id: str | None
    selected_legs: tuple[ExpressionLeg, ...]
    failure_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_utc(self.as_of, "as_of")
        if self.selected_candidate_id is None and self.selected_legs:
            raise ExpressionValidationError("unselected decision cannot contain legs")
        if self.selected_candidate_id is not None and not self.selected_legs:
            raise ExpressionValidationError("selected decision must contain legs")

    @property
    def content_hash(self) -> str:
        return _content_hash(asdict(self), label="expression-decision")


class InstrumentExpressionEngine:
    """Generate common expression forms and select a concrete sized candidate."""

    def __init__(self, policy: ExpressionPolicy) -> None:
        self._policy = policy

    def generate_candidate_groups(
        self,
        *,
        payoff: DesiredEconomicPayoff,
        instruments: Sequence[InstrumentChoice],
        as_of: datetime,
    ) -> tuple[tuple[ExpressionKind, tuple[InstrumentChoice, ...]], ...]:
        """Return PIT-safe feasible expression shapes, not preselected orders.

        Option pairs are grouped only when they share currency, expiry, and
        underlying.  This prevents an attractive-looking but economically
        incoherent spread from entering later scoring.
        """

        _require_utc(as_of, "as_of")
        eligible = tuple(
            sorted(
                (
                    item
                    for item in instruments
                    if item.known_at <= as_of
                    and item.economic_underlying_id == payoff.underlying_id
                ),
                key=lambda item: item.instrument_id,
            )
        )
        by_kind: dict[ProductKind, list[InstrumentChoice]] = {
            kind: [] for kind in ProductKind
        }
        for item in eligible:
            by_kind[item.product_kind].append(item)

        groups: list[tuple[ExpressionKind, tuple[InstrumentChoice, ...]]] = []
        allowed = set(payoff.allowed_expression_kinds)
        scalar_mapping = {
            ExpressionKind.SPOT: ProductKind.SPOT,
            ExpressionKind.ETF: ProductKind.ETF,
            ExpressionKind.FUTURE: ProductKind.FUTURE,
            ExpressionKind.CALL_OR_PUT: ProductKind.OPTION,
        }
        for expression_kind, product_kind in scalar_mapping.items():
            if expression_kind in allowed:
                groups.extend(
                    (expression_kind, (item,)) for item in by_kind[product_kind]
                )

        options = by_kind[ProductKind.OPTION]
        if ExpressionKind.OPTION_SPREAD in allowed:
            for index, first in enumerate(options):
                for second in options[index + 1 :]:
                    if (
                        first.expiry == second.expiry
                        and first.currency == second.currency
                        and first.option_right == second.option_right
                    ):
                        groups.append((ExpressionKind.OPTION_SPREAD, (first, second)))
        if ExpressionKind.SPOT_OPTION in allowed:
            groups.extend(
                (ExpressionKind.SPOT_OPTION, (spot, option))
                for spot in by_kind[ProductKind.SPOT]
                for option in options
                if spot.currency == option.currency
            )
        if ExpressionKind.FUTURE_OPTION in allowed:
            groups.extend(
                (ExpressionKind.FUTURE_OPTION, (future, option))
                for future in by_kind[ProductKind.FUTURE]
                for option in options
                if future.currency == option.currency
            )
        return tuple(groups)

    def select(
        self,
        *,
        hypothesis: EconomicHypothesis,
        payoff: DesiredEconomicPayoff,
        candidates: Iterable[ExpressionCandidate],
        as_of: datetime,
    ) -> ExpressionDecision:
        _require_utc(as_of, "as_of")
        if hypothesis.economic_underlying_id != payoff.underlying_id:
            raise ExpressionValidationError("hypothesis/payoff underlying mismatch")
        if hypothesis.expected_direction is not payoff.direction:
            raise ExpressionValidationError("hypothesis/payoff direction mismatch")

        evaluated: list[tuple[ExpressionCandidate, CandidateEvaluation]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.candidate_id in seen:
                raise ExpressionValidationError("candidate IDs must be unique")
            seen.add(candidate.candidate_id)
            evaluation = self._evaluate(candidate, payoff=payoff, as_of=as_of)
            evaluated.append((candidate, evaluation))

        feasible = [
            (candidate, evaluation)
            for candidate, evaluation in evaluated
            if evaluation.feasible and evaluation.score is not None
        ]
        feasible.sort(
            key=lambda pair: (
                -(pair[1].score or ZERO),
                pair[0].candidate_id,
            )
        )
        if not feasible:
            reasons = sorted(
                {
                    reason
                    for _, evaluation in evaluated
                    for reason in evaluation.rejection_reasons
                }
            )
            if not evaluated:
                reasons = ["NO_CANDIDATES_GENERATED"]
            return ExpressionDecision(
                hypothesis_hash=hypothesis.content_hash,
                payoff_hash=_content_hash(asdict(payoff), label="desired-payoff"),
                policy_hash=self._policy.content_hash,
                as_of=as_of,
                candidate_evaluations=tuple(item for _, item in evaluated),
                selected_candidate_id=None,
                selected_legs=(),
                failure_evidence=tuple(reasons),
            )

        selected, _ = feasible[0]
        legs = self._size(selected, payoff)
        return ExpressionDecision(
            hypothesis_hash=hypothesis.content_hash,
            payoff_hash=_content_hash(asdict(payoff), label="desired-payoff"),
            policy_hash=self._policy.content_hash,
            as_of=as_of,
            candidate_evaluations=tuple(item for _, item in evaluated),
            selected_candidate_id=selected.candidate_id,
            selected_legs=legs,
            failure_evidence=(),
        )

    def _evaluate(
        self,
        candidate: ExpressionCandidate,
        *,
        payoff: DesiredEconomicPayoff,
        as_of: datetime,
    ) -> CandidateEvaluation:
        reasons: list[str] = []
        if candidate.expression_kind not in payoff.allowed_expression_kinds:
            reasons.append("EXPRESSION_KIND_NOT_ALLOWED")
        if any(choice.known_at > as_of for choice in candidate.choices):
            reasons.append("FUTURE_KNOWLEDGE")
        if any(
            choice.economic_underlying_id != payoff.underlying_id
            for choice in candidate.choices
        ):
            reasons.append("UNDERLYING_MISMATCH")
        if candidate.liquidity_score < self._policy.minimum_liquidity_score:
            reasons.append("INSUFFICIENT_LIQUIDITY")
        if candidate.data_confidence < self._policy.minimum_data_confidence:
            reasons.append("INSUFFICIENT_DATA_CONFIDENCE")
        if candidate.maximum_loss > payoff.maximum_loss:
            reasons.append("MAXIMUM_LOSS_EXCEEDED")
        margin_fraction = candidate.margin_required / payoff.target_notional
        if margin_fraction > self._policy.maximum_margin_fraction:
            reasons.append("MARGIN_LIMIT_EXCEEDED")
        cost_fraction = candidate.transaction_cost / payoff.target_notional
        if cost_fraction > self._policy.maximum_transaction_cost_fraction:
            reasons.append("COST_LIMIT_EXCEEDED")
        if (
            payoff.maximum_premium is not None
            and candidate.targets.maximum_premium is not None
            and candidate.targets.maximum_premium > payoff.maximum_premium
        ):
            reasons.append("PREMIUM_LIMIT_EXCEEDED")
        if payoff.bounded_loss_required and candidate.maximum_loss <= ZERO:
            reasons.append("BOUNDED_LOSS_NOT_DEMONSTRATED")
        if (
            payoff.tail_protection_required
            and LegRole.TAIL_PROTECTION not in candidate.roles
        ):
            reasons.append("TAIL_PROTECTION_MISSING")

        values = (
            ("expected_return", candidate.expected_return),
            ("pnl_dispersion", candidate.pnl_dispersion),
            ("maximum_loss", candidate.maximum_loss / payoff.target_notional),
            ("carry", candidate.carry),
            ("roll_cost", candidate.roll_cost),
            ("time_value_decay", candidate.time_value_decay),
            ("implied_volatility_cost", candidate.implied_volatility_cost),
            ("liquidity_score", candidate.liquidity_score),
            ("transaction_cost", cost_fraction),
            ("margin_required", margin_fraction),
            ("tail_risk", candidate.tail_risk),
            ("model_sensitivity", candidate.model_sensitivity),
            ("data_confidence", candidate.data_confidence),
        )
        weights = self._policy.weights
        score = sum((value * weights[name] for name, value in values), ZERO)
        return CandidateEvaluation(
            candidate_id=candidate.candidate_id,
            feasible=not reasons,
            rejection_reasons=tuple(reasons),
            comparison_values=values,
            score=score if not reasons else None,
        )

    @staticmethod
    def _size(
        candidate: ExpressionCandidate,
        payoff: DesiredEconomicPayoff,
    ) -> tuple[ExpressionLeg, ...]:
        result: list[ExpressionLeg] = []
        for choice, direction, role, ratio, rule in zip(
            candidate.choices,
            candidate.directions,
            candidate.roles,
            candidate.leg_ratios,
            candidate.selection_rules,
            strict=True,
        ):
            quantity = (
                payoff.target_notional * ratio / choice.unit_notional
            ).to_integral_value(rounding=ROUND_FLOOR)
            if quantity < ONE:
                raise ExpressionValidationError(
                    "selected leg cannot reach one tradable unit within target notional"
                )
            result.append(
                ExpressionLeg(
                    selection_rule=rule,
                    instrument_id=choice.instrument_id,
                    direction=direction,
                    quantity=quantity,
                    ratio=ratio,
                    currency=choice.currency,
                    role=role,
                )
            )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class OptimizationLeg:
    """One independently selectable leg in a bounded integer search.

    Per-unit sensitivities are economic magnitudes for a long unit.  The
    optimizer applies ``direction.sign`` itself, so a caller cannot make a
    short leg appear long by pre-signing selected fields.
    """

    choice: InstrumentChoice
    selection_rule: LegSelectionRule
    direction: Direction
    role: LegRole
    minimum_quantity: int
    maximum_quantity: int
    quantity_step: int = 1
    target_ratio: Decimal = ONE
    unit_delta: Decimal = ZERO
    unit_gamma: Decimal = ZERO
    unit_vega: Decimal = ZERO
    unit_theta: Decimal = ZERO
    unit_maximum_loss: Decimal = ZERO
    unit_capital: Decimal = ZERO
    unit_margin: Decimal = ZERO
    unit_turnover: Decimal = ZERO
    concentration_group: str = "PORTFOLIO"

    def __post_init__(self) -> None:
        if not isinstance(self.choice, InstrumentChoice):
            raise ExpressionValidationError("optimization_leg_choice_required")
        if not isinstance(self.selection_rule, LegSelectionRule):
            raise ExpressionValidationError("optimization_leg_rule_required")
        if not isinstance(self.direction, Direction) or not isinstance(
            self.role, LegRole
        ):
            raise ExpressionValidationError("optimization_leg_enum_invalid")
        for name in ("minimum_quantity", "maximum_quantity", "quantity_step"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ExpressionValidationError(
                    f"optimization_leg_{name}_must_be_integer"
                )
        if (
            self.minimum_quantity < 0
            or self.maximum_quantity < self.minimum_quantity
            or self.quantity_step <= 0
        ):
            raise ExpressionValidationError("optimization_leg_quantity_range_invalid")
        if (self.maximum_quantity - self.minimum_quantity) % self.quantity_step != 0:
            raise ExpressionValidationError("optimization_leg_quantity_step_mismatch")
        if self.target_ratio <= ZERO:
            raise ExpressionValidationError("optimization_leg_target_ratio_invalid")
        for name in (
            "unit_delta",
            "unit_gamma",
            "unit_vega",
            "unit_theta",
            "unit_maximum_loss",
            "unit_capital",
            "unit_margin",
            "unit_turnover",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ExpressionValidationError(f"optimization_leg_{name}_invalid")
            if name in {
                "unit_maximum_loss",
                "unit_capital",
                "unit_margin",
                "unit_turnover",
            } and value < ZERO:
                raise ExpressionValidationError(
                    f"optimization_leg_{name}_must_be_nonnegative"
                )
        _require_text(self.concentration_group, "concentration_group")

    @property
    def quantities(self) -> tuple[int, ...]:
        return tuple(
            range(
                self.minimum_quantity,
                self.maximum_quantity + 1,
                self.quantity_step,
            )
        )


@dataclass(frozen=True, slots=True)
class JointOptimizationConstraints:
    """Strategy-wide targets and hard capital/risk boundaries."""

    target_delta: Decimal | None = None
    delta_tolerance: Decimal = ZERO
    target_gamma: Decimal | None = None
    gamma_tolerance: Decimal = ZERO
    target_vega: Decimal | None = None
    vega_tolerance: Decimal = ZERO
    target_theta: Decimal | None = None
    theta_tolerance: Decimal = ZERO
    target_notional: Decimal | None = None
    notional_tolerance: Decimal = ZERO
    maximum_loss: Decimal | None = None
    capital_limit: Decimal | None = None
    margin_limit: Decimal | None = None
    turnover_limit: Decimal | None = None
    maximum_concentration: Decimal | None = None
    minimum_liquidity_score: Decimal = ZERO
    ratio_tolerance: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in (
            "target_delta",
            "delta_tolerance",
            "target_gamma",
            "gamma_tolerance",
            "target_vega",
            "vega_tolerance",
            "target_theta",
            "theta_tolerance",
            "target_notional",
            "notional_tolerance",
            "maximum_loss",
            "capital_limit",
            "margin_limit",
            "turnover_limit",
            "maximum_concentration",
            "minimum_liquidity_score",
            "ratio_tolerance",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ExpressionValidationError(
                    f"optimization_constraints_{name}_invalid"
                )
        for name in (
            "delta_tolerance",
            "gamma_tolerance",
            "vega_tolerance",
            "theta_tolerance",
            "notional_tolerance",
            "maximum_loss",
            "capital_limit",
            "margin_limit",
            "turnover_limit",
            "ratio_tolerance",
        ):
            value = getattr(self, name)
            if value is not None and value < ZERO:
                raise ExpressionValidationError(
                    f"optimization_constraints_{name}_must_be_nonnegative"
                )
        for name in ("maximum_concentration", "minimum_liquidity_score"):
            value = getattr(self, name)
            if value is not None:
                _require_fraction(value, name)


@dataclass(frozen=True, slots=True)
class JointOptimizationProblem:
    problem_id: str
    hypothesis_hash: str
    payoff_hash: str
    policy_hash: str
    as_of: datetime
    legs: tuple[OptimizationLeg, ...]
    constraints: JointOptimizationConstraints
    maximum_combinations: int = 250_000
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("problem_id", "hypothesis_hash", "payoff_hash", "policy_hash"):
            _require_text(getattr(self, name), name)
        _require_utc(self.as_of, "as_of")
        if not self.legs:
            raise ExpressionValidationError("optimization_problem_legs_required")
        ids = [item.choice.instrument_id for item in self.legs]
        if len(ids) != len(set(ids)):
            raise ExpressionValidationError(
                "optimization_problem_instrument_ids_duplicate"
            )
        if not isinstance(self.constraints, JointOptimizationConstraints):
            raise ExpressionValidationError(
                "optimization_problem_constraints_required"
            )
        if (
            isinstance(self.maximum_combinations, bool)
            or not isinstance(self.maximum_combinations, int)
            or self.maximum_combinations <= 0
        ):
            raise ExpressionValidationError(
                "optimization_problem_maximum_combinations_invalid"
            )
        combinations = 1
        for leg in self.legs:
            combinations *= len(leg.quantities)
            if combinations > self.maximum_combinations:
                raise ExpressionValidationError(
                    "optimization_problem_combination_limit_exceeded"
                )
        expected = _content_hash(
            {
                "problem_id": self.problem_id,
                "hypothesis_hash": self.hypothesis_hash,
                "payoff_hash": self.payoff_hash,
                "policy_hash": self.policy_hash,
                "as_of": self.as_of,
                "legs": [asdict(item) for item in self.legs],
                "constraints": asdict(self.constraints),
                "maximum_combinations": self.maximum_combinations,
            },
            label="joint-optimization-problem",
        )
        if self.content_hash and self.content_hash != expected:
            raise ExpressionValidationError(
                "optimization_problem_content_hash_mismatch"
            )
        object.__setattr__(self, "content_hash", expected)


@dataclass(frozen=True, slots=True)
class OptimizationMetrics:
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    gross_notional: Decimal
    maximum_loss: Decimal
    capital: Decimal
    margin: Decimal
    turnover: Decimal
    concentration: Decimal
    minimum_liquidity_score: Decimal

    def as_pairs(self) -> tuple[tuple[str, Decimal], ...]:
        return tuple(
            (name, getattr(self, name))
            for name in (
                "delta",
                "gamma",
                "vega",
                "theta",
                "gross_notional",
                "maximum_loss",
                "capital",
                "margin",
                "turnover",
                "concentration",
                "minimum_liquidity_score",
            )
        )


@dataclass(frozen=True, slots=True)
class JointOptimizationResult:
    problem_hash: str
    hypothesis_hash: str
    payoff_hash: str
    policy_hash: str
    feasible: bool
    quantities: tuple[tuple[str, int], ...]
    selected_legs: tuple[ExpressionLeg, ...]
    metrics: OptimizationMetrics | None
    objective: Decimal | None
    evaluated_combinations: int
    infeasibility_reasons: tuple[tuple[str, int], ...]
    hypothesis_feedback: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        for name in (
            "problem_hash",
            "hypothesis_hash",
            "payoff_hash",
            "policy_hash",
            "content_hash",
        ):
            _require_text(getattr(self, name), f"optimization_result.{name}")
        if self.feasible != bool(self.selected_legs):
            raise ExpressionValidationError("optimization_result_feasibility_mismatch")
        if self.feasible != (self.metrics is not None and self.objective is not None):
            raise ExpressionValidationError("optimization_result_metrics_mismatch")

    def as_expression_decision(self, *, as_of: datetime) -> ExpressionDecision:
        _require_utc(as_of, "as_of")
        if self.feasible and self.metrics is not None:
            evaluation = CandidateEvaluation(
                candidate_id=self.problem_hash,
                feasible=True,
                rejection_reasons=(),
                comparison_values=self.metrics.as_pairs(),
                score=-self.objective if self.objective is not None else ZERO,
            )
            return ExpressionDecision(
                hypothesis_hash=self.hypothesis_hash,
                payoff_hash=self.payoff_hash,
                policy_hash=self.policy_hash,
                as_of=as_of,
                candidate_evaluations=(evaluation,),
                selected_candidate_id=self.problem_hash,
                selected_legs=self.selected_legs,
                failure_evidence=(),
            )
        evaluation = CandidateEvaluation(
            candidate_id=self.problem_hash,
            feasible=False,
            rejection_reasons=tuple(key for key, _ in self.infeasibility_reasons),
            comparison_values=(),
            score=None,
        )
        return ExpressionDecision(
            hypothesis_hash=self.hypothesis_hash,
            payoff_hash=self.payoff_hash,
            policy_hash=self.policy_hash,
            as_of=as_of,
            candidate_evaluations=(evaluation,),
            selected_candidate_id=None,
            selected_legs=(),
            failure_evidence=self.hypothesis_feedback,
        )


class DeterministicJointOptimizer:
    """Exhaustive bounded integer optimizer with stable tie breaking."""

    def optimize(self, problem: JointOptimizationProblem) -> JointOptimizationResult:
        if not isinstance(problem, JointOptimizationProblem):
            raise ExpressionValidationError("joint_optimizer_problem_required")
        static_reasons: dict[str, int] = {}
        for leg in problem.legs:
            for reason in _selection_rule_reasons(
                leg.choice,
                leg.selection_rule,
                as_of=problem.as_of,
            ):
                static_reasons[reason] = static_reasons.get(reason, 0) + 1
        if static_reasons:
            return _infeasible_optimization_result(
                problem, static_reasons, evaluated=0
            )

        rejection_counts: dict[str, int] = {}
        feasible: list[
            tuple[
                Decimal,
                tuple[int, ...],
                OptimizationMetrics,
            ]
        ] = []
        evaluated = 0
        for quantities in product(*(leg.quantities for leg in problem.legs)):
            evaluated += 1
            if not any(quantities):
                _count_reasons(rejection_counts, ("EMPTY_PORTFOLIO",))
                continue
            metrics = _optimization_metrics(problem.legs, quantities)
            reasons = _constraint_reasons(
                problem.legs,
                quantities,
                metrics,
                problem.constraints,
            )
            if reasons:
                _count_reasons(rejection_counts, reasons)
                continue
            objective = _optimization_objective(metrics, problem.constraints)
            feasible.append((objective, tuple(quantities), metrics))
        if not feasible:
            return _infeasible_optimization_result(
                problem, rejection_counts, evaluated=evaluated
            )
        objective, quantities, metrics = min(
            feasible,
            key=lambda item: (
                item[0],
                sum(item[1]),
                item[1],
                tuple(leg.choice.instrument_id for leg in problem.legs),
            ),
        )
        selected = tuple(
            ExpressionLeg(
                selection_rule=leg.selection_rule,
                instrument_id=leg.choice.instrument_id,
                direction=leg.direction,
                quantity=Decimal(quantity),
                ratio=leg.target_ratio,
                currency=leg.choice.currency,
                role=leg.role,
            )
            for leg, quantity in zip(problem.legs, quantities, strict=True)
            if quantity > 0
        )
        quantity_pairs = tuple(
            (leg.choice.instrument_id, quantity)
            for leg, quantity in zip(problem.legs, quantities, strict=True)
        )
        payload = {
            "problem_hash": problem.content_hash,
            "feasible": True,
            "quantities": quantity_pairs,
            "metrics": metrics.as_pairs(),
            "objective": objective,
            "evaluated_combinations": evaluated,
        }
        result_hash = _content_hash(payload, label="joint-optimization-result")
        return JointOptimizationResult(
            problem_hash=problem.content_hash,
            hypothesis_hash=problem.hypothesis_hash,
            payoff_hash=problem.payoff_hash,
            policy_hash=problem.policy_hash,
            feasible=True,
            quantities=quantity_pairs,
            selected_legs=selected,
            metrics=metrics,
            objective=objective,
            evaluated_combinations=evaluated,
            infeasibility_reasons=(),
            hypothesis_feedback=(),
            content_hash=result_hash,
        )


def _selection_rule_reasons(
    choice: InstrumentChoice,
    rule: LegSelectionRule,
    *,
    as_of: datetime,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if choice.product_kind is not rule.product_kind:
        reasons.append("LEG_PRODUCT_KIND_MISMATCH")
    if choice.known_at > as_of:
        reasons.append("LEG_FUTURE_KNOWLEDGE")
    if choice.liquidity_score < rule.minimum_liquidity_score:
        reasons.append("LEG_LIQUIDITY_BELOW_RULE")
    if choice.expiry is not None:
        days = (choice.expiry - as_of).total_seconds() / 86_400
        if (
            rule.minimum_days_to_expiry is not None
            and days < rule.minimum_days_to_expiry
        ):
            reasons.append("LEG_EXPIRY_BELOW_RULE")
        if (
            rule.maximum_days_to_expiry is not None
            and days > rule.maximum_days_to_expiry
        ):
            reasons.append("LEG_EXPIRY_ABOVE_RULE")
    elif (
        rule.minimum_days_to_expiry is not None
        or rule.maximum_days_to_expiry is not None
    ):
        reasons.append("LEG_EXPIRY_REQUIRED_BY_RULE")
    if rule.target_delta is not None and choice.delta != rule.target_delta:
        reasons.append("LEG_DELTA_TARGET_MISMATCH")
    if rule.target_vega is not None and choice.vega != rule.target_vega:
        reasons.append("LEG_VEGA_TARGET_MISMATCH")
    if rule.target_moneyness is not None:
        underlying_reference = (
            choice.economic_notional_per_unit / choice.contract_multiplier
        )
        if choice.strike is None or underlying_reference == ZERO:
            reasons.append("LEG_MONEYNESS_UNAVAILABLE")
        elif choice.strike / underlying_reference != rule.target_moneyness:
            reasons.append("LEG_MONEYNESS_TARGET_MISMATCH")
    return tuple(sorted(set(reasons)))


def _optimization_metrics(
    legs: tuple[OptimizationLeg, ...],
    quantities: tuple[int, ...],
) -> OptimizationMetrics:
    def signed(name: str) -> Decimal:
        return sum(
            (
                Decimal(quantity) * leg.direction.sign * getattr(leg, name)
                for leg, quantity in zip(legs, quantities, strict=True)
            ),
            ZERO,
        )

    gross_by_group: dict[str, Decimal] = {}
    gross = ZERO
    minimum_liquidity = ONE
    for leg, quantity in zip(legs, quantities, strict=True):
        if quantity <= 0:
            continue
        leg_notional = Decimal(quantity) * leg.choice.unit_notional
        gross += leg_notional
        gross_by_group[leg.concentration_group] = (
            gross_by_group.get(leg.concentration_group, ZERO) + leg_notional
        )
        minimum_liquidity = min(minimum_liquidity, leg.choice.liquidity_score)
    concentration = (
        max(gross_by_group.values(), default=ZERO) / gross if gross > ZERO else ZERO
    )
    return OptimizationMetrics(
        delta=signed("unit_delta"),
        gamma=signed("unit_gamma"),
        vega=signed("unit_vega"),
        theta=signed("unit_theta"),
        gross_notional=gross,
        maximum_loss=sum(
            (
                Decimal(quantity) * leg.unit_maximum_loss
                for leg, quantity in zip(legs, quantities, strict=True)
            ),
            ZERO,
        ),
        capital=sum(
            (
                Decimal(quantity) * leg.unit_capital
                for leg, quantity in zip(legs, quantities, strict=True)
            ),
            ZERO,
        ),
        margin=sum(
            (
                Decimal(quantity) * leg.unit_margin
                for leg, quantity in zip(legs, quantities, strict=True)
            ),
            ZERO,
        ),
        turnover=sum(
            (
                Decimal(quantity) * leg.unit_turnover
                for leg, quantity in zip(legs, quantities, strict=True)
            ),
            ZERO,
        ),
        concentration=concentration,
        minimum_liquidity_score=minimum_liquidity,
    )


def _constraint_reasons(
    legs: tuple[OptimizationLeg, ...],
    quantities: tuple[int, ...],
    metrics: OptimizationMetrics,
    constraints: JointOptimizationConstraints,
) -> tuple[str, ...]:
    reasons: list[str] = []
    for metric_name, target_name, tolerance_name in (
        ("delta", "target_delta", "delta_tolerance"),
        ("gamma", "target_gamma", "gamma_tolerance"),
        ("vega", "target_vega", "vega_tolerance"),
        ("theta", "target_theta", "theta_tolerance"),
        ("gross_notional", "target_notional", "notional_tolerance"),
    ):
        target = getattr(constraints, target_name)
        if target is not None and abs(getattr(metrics, metric_name) - target) > getattr(
            constraints, tolerance_name
        ):
            reasons.append(f"{metric_name.upper()}_TARGET_UNSATISFIED")
    for metric_name, limit_name in (
        ("maximum_loss", "maximum_loss"),
        ("capital", "capital_limit"),
        ("margin", "margin_limit"),
        ("turnover", "turnover_limit"),
        ("concentration", "maximum_concentration"),
    ):
        limit = getattr(constraints, limit_name)
        if limit is not None and getattr(metrics, metric_name) > limit:
            reasons.append(f"{metric_name.upper()}_LIMIT_EXCEEDED")
    if metrics.minimum_liquidity_score < constraints.minimum_liquidity_score:
        reasons.append("LIQUIDITY_LIMIT_UNSATISFIED")
    active = [
        (Decimal(quantity) / leg.target_ratio)
        for leg, quantity in zip(legs, quantities, strict=True)
        if quantity > 0
    ]
    if active and max(active) - min(active) > constraints.ratio_tolerance:
        reasons.append("LEG_RATIO_UNSATISFIED")
    return tuple(sorted(set(reasons)))


def _optimization_objective(
    metrics: OptimizationMetrics,
    constraints: JointOptimizationConstraints,
) -> Decimal:
    residual = ZERO
    for metric_name, target_name in (
        ("delta", "target_delta"),
        ("gamma", "target_gamma"),
        ("vega", "target_vega"),
        ("theta", "target_theta"),
        ("gross_notional", "target_notional"),
    ):
        target = getattr(constraints, target_name)
        if target is not None:
            residual += abs(getattr(metrics, metric_name) - target)
    return (
        residual
        + metrics.maximum_loss
        + metrics.margin
        + metrics.turnover
        + metrics.concentration
    )


def _count_reasons(counts: dict[str, int], reasons: Iterable[str]) -> None:
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1


def _infeasible_optimization_result(
    problem: JointOptimizationProblem,
    counts: Mapping[str, int],
    *,
    evaluated: int,
) -> JointOptimizationResult:
    reasons = tuple(sorted(counts.items()))
    feedback_map = {
        "DELTA_TARGET_UNSATISFIED": "HYPOTHESIS_DELTA_EXPRESSION_INFEASIBLE",
        "GAMMA_TARGET_UNSATISFIED": "HYPOTHESIS_CONVEXITY_EXPRESSION_INFEASIBLE",
        "VEGA_TARGET_UNSATISFIED": "HYPOTHESIS_VOLATILITY_EXPRESSION_INFEASIBLE",
        "MAXIMUM_LOSS_LIMIT_EXCEEDED": "HYPOTHESIS_DOWNSIDE_BUDGET_INFEASIBLE",
        "CAPITAL_LIMIT_EXCEEDED": "HYPOTHESIS_CAPITAL_BUDGET_INFEASIBLE",
        "MARGIN_LIMIT_EXCEEDED": "HYPOTHESIS_MARGIN_BUDGET_INFEASIBLE",
        "LIQUIDITY_LIMIT_UNSATISFIED": "HYPOTHESIS_CAPACITY_INFEASIBLE",
    }
    feedback = tuple(
        sorted(
            {
                feedback_map.get(reason, f"HYPOTHESIS_EXPRESSION_FAILED:{reason}")
                for reason, _count in reasons
            }
        )
    )
    payload = {
        "problem_hash": problem.content_hash,
        "feasible": False,
        "evaluated_combinations": evaluated,
        "infeasibility_reasons": reasons,
        "hypothesis_feedback": feedback,
    }
    return JointOptimizationResult(
        problem_hash=problem.content_hash,
        hypothesis_hash=problem.hypothesis_hash,
        payoff_hash=problem.payoff_hash,
        policy_hash=problem.policy_hash,
        feasible=False,
        quantities=(),
        selected_legs=(),
        metrics=None,
        objective=None,
        evaluated_combinations=evaluated,
        infeasibility_reasons=reasons,
        hypothesis_feedback=feedback,
        content_hash=_content_hash(payload, label="joint-optimization-result"),
    )


DEFAULT_EXPRESSION_POLICY = ExpressionPolicy(
    policy_id="common-expression-v1",
    version="1.0.0",
    minimum_liquidity_score=Decimal("0.25"),
    minimum_data_confidence=Decimal("0.50"),
    maximum_margin_fraction=Decimal("1.0"),
    maximum_transaction_cost_fraction=Decimal("0.10"),
    score_weights=(
        ("expected_return", Decimal("1.0")),
        ("pnl_dispersion", Decimal("-0.20")),
        ("maximum_loss", Decimal("-0.50")),
        ("carry", Decimal("0.25")),
        ("roll_cost", Decimal("-0.25")),
        ("time_value_decay", Decimal("-0.25")),
        ("implied_volatility_cost", Decimal("-0.20")),
        ("liquidity_score", Decimal("0.20")),
        ("transaction_cost", Decimal("-0.50")),
        ("margin_required", Decimal("-0.10")),
        ("tail_risk", Decimal("-0.50")),
        ("model_sensitivity", Decimal("-0.20")),
        ("data_confidence", Decimal("0.30")),
    ),
)


__all__ = [
    "CandidateEvaluation",
    "DEFAULT_EXPRESSION_POLICY",
    "DesiredEconomicPayoff",
    "Direction",
    "EconomicHypothesis",
    "ExecutionMode",
    "ExpectedMarketDistribution",
    "ExpressionCandidate",
    "ExpressionDecision",
    "ExpressionKind",
    "ExpressionLeg",
    "ExpressionPolicy",
    "ExpressionValidationError",
    "InstrumentChoice",
    "InstrumentExpressionEngine",
    "JointOptimizationConstraints",
    "JointOptimizationProblem",
    "JointOptimizationResult",
    "LegRole",
    "LegSelectionRule",
    "LegState",
    "OptimizationLeg",
    "OptimizationMetrics",
    "ProductKind",
    "ScenarioRange",
    "StrategyTargets",
    "DeterministicJointOptimizer",
]
