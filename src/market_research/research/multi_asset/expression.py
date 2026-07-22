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
    "LegRole",
    "LegSelectionRule",
    "LegState",
    "ProductKind",
    "ScenarioRange",
    "StrategyTargets",
]
