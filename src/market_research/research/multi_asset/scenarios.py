"""Joint, immutable market shocks and portfolio repricing.

The engine consumes the shared market-state contract structurally.  It never
modifies that state: a shock produces a separate hash-bound view whose parent
hash is retained in the scenario evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Protocol, runtime_checkable

from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    PortfolioAccountingError,
    PortfolioSnapshot,
    PortfolioValuation,
    PositionView,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_HARD_MAX_PATH_STEPS = 1_024


class ScenarioError(ValueError):
    """Raised when a scenario is incomplete or dimensionally ambiguous."""


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ScenarioError(f"{field_name}_must_be_decimal")
    if not value.is_finite():
        raise ScenarioError(f"{field_name}_must_be_finite")
    if positive and value <= _ZERO:
        raise ScenarioError(f"{field_name}_must_be_positive")
    if nonnegative and value < _ZERO:
        raise ScenarioError(f"{field_name}_must_be_nonnegative")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == _ZERO:
        return "0"
    return format(value.normalize(), "f")


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ScenarioError(f"{field_name}_invalid")


def _require_hash(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
    ):
        raise ScenarioError(f"{field_name}_invalid")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ScenarioError(f"{field_name}_invalid") from exc


def _timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ScenarioError(f"{field_name}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScenarioError(f"{field_name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: str, field_name: str) -> str:
    return _timestamp(value, field_name).isoformat()


def _normalize_pairs(
    values: tuple[tuple[str, Decimal], ...],
    field_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> tuple[tuple[str, Decimal], ...]:
    normalized = tuple(sorted(values))
    if len({key for key, _ in normalized}) != len(normalized):
        raise ScenarioError(f"{field_name}_duplicate")
    for key, value in normalized:
        _require_id(key, f"{field_name}.key")
        _decimal(
            value,
            f"{field_name}.value",
            positive=positive,
            nonnegative=nonnegative,
        )
    return normalized


@runtime_checkable
class SpotQuoteLike(Protocol):
    price: Decimal


@runtime_checkable
class ImmutableMarketStateLike(Protocol):
    """Narrow structural boundary implemented by ``multi_asset.MarketState``."""

    state_id: str
    valuation_at: str
    base_currency: str

    def state_hash(self) -> str: ...

    def spot_price(self, instrument_id: str) -> SpotQuoteLike: ...

    def convert(
        self, amount: Decimal, *, from_currency: str, to_currency: str
    ) -> Decimal: ...


@runtime_checkable
class PositionRepricer(Protocol):
    """Product-specific reprice boundary for futures or nonlinear options."""

    def reprice(
        self,
        position: PositionView,
        *,
        market_state: ImmutableMarketStateLike,
        shocked_state: ShockedMarketState,
    ) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class JointMarketShock:
    """Correlated deterministic shock across prices and common risk factors."""

    scenario_id: str
    price_returns: tuple[tuple[str, Decimal], ...] = ()
    price_absolute_shifts: tuple[tuple[str, Decimal], ...] = ()
    fx_returns: tuple[tuple[str, Decimal], ...] = ()
    volatility_shifts: tuple[tuple[str, Decimal], ...] = ()
    rate_shifts: tuple[tuple[str, Decimal], ...] = ()
    liquidity_haircuts: tuple[tuple[str, Decimal], ...] = ()
    liquidity_cost_multiplier: Decimal = Decimal("1")
    margin_multiplier: Decimal = Decimal("1")
    source_hashes: tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.scenario_id, "scenario.scenario_id")
        for field_name in (
            "price_returns",
            "price_absolute_shifts",
            "fx_returns",
            "volatility_shifts",
            "rate_shifts",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_pairs(getattr(self, field_name), f"scenario.{field_name}"),
            )
        haircuts = _normalize_pairs(
            self.liquidity_haircuts,
            "scenario.liquidity_haircuts",
            nonnegative=True,
        )
        if any(value > _ONE for _, value in haircuts):
            raise ScenarioError("scenario_liquidity_haircut_above_one")
        object.__setattr__(self, "liquidity_haircuts", haircuts)
        _decimal(
            self.liquidity_cost_multiplier,
            "scenario.liquidity_cost_multiplier",
            positive=True,
        )
        _decimal(
            self.margin_multiplier,
            "scenario.margin_multiplier",
            positive=True,
        )
        sources = tuple(sorted(set(self.source_hashes)))
        if sources != self.source_hashes:
            object.__setattr__(self, "source_hashes", sources)
        for source_hash in sources:
            _require_hash(source_hash, "scenario.source_hash")
        returns = dict(self.price_returns)
        absolute = dict(self.price_absolute_shifts)
        overlap = set(returns) & set(absolute)
        if overlap:
            raise ScenarioError(
                "scenario_price_shock_ambiguous:" + ",".join(sorted(overlap))
            )
        if any(value <= -_ONE for value in returns.values()):
            raise ScenarioError("scenario_price_return_at_or_below_minus_one")
        if any(value <= -_ONE for _, value in self.fx_returns):
            raise ScenarioError("scenario_fx_return_at_or_below_minus_one")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="joint_market_shock"),
        )

    def identity_payload(self) -> dict[str, object]:
        def pairs(values: tuple[tuple[str, Decimal], ...]) -> list[dict[str, str]]:
            return [
                {"key": key, "value": _decimal_text(value)} for key, value in values
            ]

        return {
            "scenario_id": self.scenario_id,
            "price_returns": pairs(self.price_returns),
            "price_absolute_shifts": pairs(self.price_absolute_shifts),
            "fx_returns": pairs(self.fx_returns),
            "volatility_shifts": pairs(self.volatility_shifts),
            "rate_shifts": pairs(self.rate_shifts),
            "liquidity_haircuts": pairs(self.liquidity_haircuts),
            "liquidity_cost_multiplier": _decimal_text(self.liquidity_cost_multiplier),
            "margin_multiplier": _decimal_text(self.margin_multiplier),
            "source_hashes": list(self.source_hashes),
        }


@dataclass(frozen=True, slots=True)
class ShockedMarketState:
    """Immutable derived state containing only scenario-adjusted projections."""

    parent_state_id: str
    parent_state_hash: str
    valuation_at: str
    base_currency: str
    scenario_hash: str
    prices: tuple[tuple[str, Decimal], ...]
    fx_rates: tuple[tuple[str, Decimal], ...]
    volatility_shifts: tuple[tuple[str, Decimal], ...]
    rate_shifts: tuple[tuple[str, Decimal], ...]
    liquidity_haircuts: tuple[tuple[str, Decimal], ...]
    liquidity_cost_multiplier: Decimal
    margin_multiplier: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.parent_state_id, "shocked_state.parent_state_id")
        _require_hash(self.parent_state_hash, "shocked_state.parent_state_hash")
        _require_hash(self.scenario_hash, "shocked_state.scenario_hash")
        object.__setattr__(
            self,
            "valuation_at",
            _timestamp_text(self.valuation_at, "shocked_state.valuation_at"),
        )
        _require_id(self.base_currency, "shocked_state.base_currency")
        object.__setattr__(
            self,
            "prices",
            _normalize_pairs(self.prices, "shocked_state.prices", positive=True),
        )
        object.__setattr__(
            self,
            "fx_rates",
            _normalize_pairs(self.fx_rates, "shocked_state.fx_rates", positive=True),
        )
        for field_name in (
            "volatility_shifts",
            "rate_shifts",
            "liquidity_haircuts",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_pairs(
                    getattr(self, field_name), f"shocked_state.{field_name}"
                ),
            )
        _decimal(
            self.liquidity_cost_multiplier,
            "shocked_state.liquidity_cost_multiplier",
            positive=True,
        )
        _decimal(
            self.margin_multiplier,
            "shocked_state.margin_multiplier",
            positive=True,
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="shocked_market_state"),
        )

    def price_for(self, instrument_id: str) -> Decimal:
        try:
            return dict(self.prices)[instrument_id]
        except KeyError as exc:
            raise ScenarioError(f"scenario_price_missing:{instrument_id}") from exc

    def fx_rate_for(self, currency: str) -> Decimal:
        try:
            return dict(self.fx_rates)[currency]
        except KeyError as exc:
            raise ScenarioError(f"scenario_fx_rate_missing:{currency}") from exc

    def identity_payload(self) -> dict[str, object]:
        def pairs(values: tuple[tuple[str, Decimal], ...]) -> list[dict[str, str]]:
            return [
                {"key": key, "value": _decimal_text(value)} for key, value in values
            ]

        return {
            "parent_state_id": self.parent_state_id,
            "parent_state_hash": self.parent_state_hash,
            "valuation_at": self.valuation_at,
            "base_currency": self.base_currency,
            "scenario_hash": self.scenario_hash,
            "prices": pairs(self.prices),
            "fx_rates": pairs(self.fx_rates),
            "volatility_shifts": pairs(self.volatility_shifts),
            "rate_shifts": pairs(self.rate_shifts),
            "liquidity_haircuts": pairs(self.liquidity_haircuts),
            "liquidity_cost_multiplier": _decimal_text(self.liquidity_cost_multiplier),
            "margin_multiplier": _decimal_text(self.margin_multiplier),
        }


@dataclass(frozen=True, slots=True)
class ScenarioPositionResult:
    instrument_id: str
    asset_class: AssetClass
    base_mark: Decimal
    shocked_mark: Decimal
    base_value: Decimal
    shocked_value: Decimal
    pnl_change: Decimal
    repricer: str


@dataclass(frozen=True, slots=True)
class JointScenarioResult:
    scenario_id: str
    scenario_hash: str
    base_state_hash: str
    shocked_state_hash: str
    shocked_state: ShockedMarketState
    ledger_hash: str
    base_valuation: PortfolioValuation
    shocked_valuation: PortfolioValuation
    position_results: tuple[ScenarioPositionResult, ...]
    liquidity_reserve: Decimal
    nav_change: Decimal
    available_capital_change: Decimal
    original_state_unchanged: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "scenario_hash",
            "base_state_hash",
            "shocked_state_hash",
            "ledger_hash",
        ):
            _require_hash(getattr(self, field_name), f"scenario_result.{field_name}")
        if self.shocked_state.content_hash != self.shocked_state_hash:
            raise ScenarioError("scenario_result_shocked_state_hash_mismatch")
        if self.shocked_state.parent_state_hash != self.base_state_hash:
            raise ScenarioError("scenario_result_parent_state_hash_mismatch")
        for field_name in (
            "liquidity_reserve",
            "nav_change",
            "available_capital_change",
        ):
            _decimal(getattr(self, field_name), f"scenario_result.{field_name}")
        if not self.base_valuation.reconciled or not self.shocked_valuation.reconciled:
            raise ScenarioError("scenario_result_portfolio_not_reconciled")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="joint_scenario_result"),
        )

    def identity_payload(self) -> dict[str, object]:
        def valuation_payload(value: PortfolioValuation) -> dict[str, str]:
            return {
                "nav": _decimal_text(value.nav),
                "economic_pnl": _decimal_text(value.economic_pnl),
                "available_capital": _decimal_text(value.available_capital),
                "reconciliation_error": _decimal_text(value.reconciliation_error),
            }

        return {
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "base_state_hash": self.base_state_hash,
            "shocked_state_hash": self.shocked_state_hash,
            "ledger_hash": self.ledger_hash,
            "base_valuation": valuation_payload(self.base_valuation),
            "shocked_valuation": valuation_payload(self.shocked_valuation),
            "position_results": [
                {
                    "instrument_id": item.instrument_id,
                    "asset_class": item.asset_class.value,
                    "base_mark": _decimal_text(item.base_mark),
                    "shocked_mark": _decimal_text(item.shocked_mark),
                    "base_value": _decimal_text(item.base_value),
                    "shocked_value": _decimal_text(item.shocked_value),
                    "pnl_change": _decimal_text(item.pnl_change),
                    "repricer": item.repricer,
                }
                for item in self.position_results
            ],
            "liquidity_reserve": _decimal_text(self.liquidity_reserve),
            "nav_change": _decimal_text(self.nav_change),
            "available_capital_change": _decimal_text(self.available_capital_change),
            "original_state_unchanged": self.original_state_unchanged,
        }


@dataclass(frozen=True, slots=True)
class JointScenarioEngine:
    """Shock all held products against one valuation-time state."""

    require_nonlinear_option_repricing: bool = True

    def evaluate(
        self,
        snapshot: PortfolioSnapshot,
        *,
        market_state: ImmutableMarketStateLike,
        shock: JointMarketShock,
        repricers: Mapping[str, PositionRepricer] | None = None,
        base_liquidation_costs: Mapping[str, Decimal] | None = None,
        scenario_valuation_at: str | None = None,
    ) -> JointScenarioResult:
        if snapshot.base_currency != market_state.base_currency:
            raise ScenarioError("scenario_market_state_base_currency_mismatch")
        repricer_by_instrument = repricers or {}
        base_costs = base_liquidation_costs or {}
        base_state_hash = market_state.state_hash()
        _require_hash(base_state_hash, "scenario.market_state_hash")
        effective_valuation_at = (
            market_state.valuation_at
            if scenario_valuation_at is None
            else _timestamp_text(
                scenario_valuation_at,
                "scenario.scenario_valuation_at",
            )
        )
        if _timestamp(
            effective_valuation_at,
            "scenario.scenario_valuation_at",
        ) < _timestamp(market_state.valuation_at, "scenario.market_state.valuation_at"):
            raise ScenarioError("scenario_valuation_before_market_state")
        require_usable = getattr(market_state, "require_usable", None)
        if callable(require_usable):
            require_usable()

        base_marks = self._base_marks(snapshot, market_state)
        base_fx = self._base_fx(snapshot, market_state)
        shocked_state = self._shock_state(
            market_state=market_state,
            base_state_hash=base_state_hash,
            base_marks=base_marks,
            base_fx=base_fx,
            shock=shock,
            valuation_at=effective_valuation_at,
        )
        shocked_marks = dict(shocked_state.prices)
        return_shocks = set(dict(shock.price_returns))
        absolute_shocks = set(dict(shock.price_absolute_shifts))
        factor_shock_present = bool(shock.volatility_shifts or shock.rate_shifts)
        repricer_names: dict[str, str] = {}
        for position in snapshot.positions:
            repricer = repricer_by_instrument.get(position.instrument_id)
            if repricer is not None:
                shocked_mark = repricer.reprice(
                    position,
                    market_state=market_state,
                    shocked_state=shocked_state,
                )
                _decimal(shocked_mark, "scenario.repriced_mark", positive=True)
                shocked_marks[position.instrument_id] = shocked_mark
                repricer_names[position.instrument_id] = type(repricer).__name__
            elif (
                position.asset_class is AssetClass.OPTION
                and self.require_nonlinear_option_repricing
                and factor_shock_present
                and position.instrument_id not in return_shocks
                and position.instrument_id not in absolute_shocks
            ):
                raise ScenarioError(
                    f"scenario_option_repricer_required:{position.instrument_id}"
                )
            else:
                repricer_names[position.instrument_id] = "direct_mark_shock"

        haircuts = dict(shock.liquidity_haircuts)
        for position in snapshot.positions:
            haircut = haircuts.get(position.instrument_id, _ZERO)
            if haircut == _ZERO:
                continue
            mark = shocked_marks[position.instrument_id]
            shocked_marks[position.instrument_id] = mark * (
                (_ONE - haircut) if position.quantity > _ZERO else (_ONE + haircut)
            )
            if shocked_marks[position.instrument_id] <= _ZERO:
                raise ScenarioError(
                    f"scenario_liquidity_mark_nonpositive:{position.instrument_id}"
                )

        liquidity_reserve = _ZERO
        for instrument_id, cost in base_costs.items():
            _require_id(instrument_id, "scenario.liquidation_cost.instrument_id")
            _decimal(cost, "scenario.liquidation_cost", nonnegative=True)
            liquidity_reserve += cost * shock.liquidity_cost_multiplier

        try:
            base_valuation = snapshot.valuation(
                fx_rates=base_fx,
                marks=base_marks,
            )
            shocked_valuation = snapshot.valuation(
                fx_rates=dict(shocked_state.fx_rates),
                marks=shocked_marks,
                margin_multiplier=shock.margin_multiplier,
                liquidity_reserve=liquidity_reserve,
            )
        except PortfolioAccountingError as exc:
            raise ScenarioError(str(exc)) from exc

        position_results: list[ScenarioPositionResult] = []
        for position in snapshot.positions:
            base_mark = base_marks[position.instrument_id]
            shocked_mark = shocked_marks[position.instrument_id]
            base_rate = base_fx[position.currency]
            shocked_rate = dict(shocked_state.fx_rates)[position.currency]
            base_value = position.market_value(base_mark) * base_rate
            shocked_value = position.market_value(shocked_mark) * shocked_rate
            position_results.append(
                ScenarioPositionResult(
                    instrument_id=position.instrument_id,
                    asset_class=position.asset_class,
                    base_mark=base_mark,
                    shocked_mark=shocked_mark,
                    base_value=base_value,
                    shocked_value=shocked_value,
                    pnl_change=shocked_value - base_value,
                    repricer=repricer_names[position.instrument_id],
                )
            )
        unchanged = market_state.state_hash() == base_state_hash
        return JointScenarioResult(
            scenario_id=shock.scenario_id,
            scenario_hash=shock.content_hash,
            base_state_hash=base_state_hash,
            shocked_state_hash=shocked_state.content_hash,
            shocked_state=shocked_state,
            ledger_hash=snapshot.ledger_hash,
            base_valuation=base_valuation,
            shocked_valuation=shocked_valuation,
            position_results=tuple(
                sorted(position_results, key=lambda item: item.instrument_id)
            ),
            liquidity_reserve=liquidity_reserve,
            nav_change=shocked_valuation.nav - base_valuation.nav,
            available_capital_change=(
                shocked_valuation.available_capital - base_valuation.available_capital
            ),
            original_state_unchanged=unchanged,
        )

    @staticmethod
    def _base_marks(
        snapshot: PortfolioSnapshot,
        market_state: ImmutableMarketStateLike,
    ) -> dict[str, Decimal]:
        marks: dict[str, Decimal] = {}
        for position in snapshot.positions:
            mark = position.mark_price
            if position.asset_class is AssetClass.SPOT:
                try:
                    mark = market_state.spot_price(position.instrument_id).price
                except (KeyError, ValueError):
                    # A held synthetic/derived spot may be marked by the ledger.
                    mark = position.mark_price
            _decimal(mark, "scenario.base_mark", positive=True)
            marks[position.instrument_id] = mark
        return marks

    @staticmethod
    def _base_fx(
        snapshot: PortfolioSnapshot,
        market_state: ImmutableMarketStateLike,
    ) -> dict[str, Decimal]:
        currencies = {snapshot.base_currency}
        currencies.update(item.currency for item in snapshot.cash)
        currencies.update(item.currency for item in snapshot.collateral)
        currencies.update(item.currency for item in snapshot.margins)
        currencies.update(item.currency for item in snapshot.positions)
        rates: dict[str, Decimal] = {}
        for currency in sorted(currencies):
            rate = market_state.convert(
                _ONE,
                from_currency=currency,
                to_currency=snapshot.base_currency,
            )
            _decimal(rate, "scenario.base_fx_rate", positive=True)
            rates[currency] = rate
        return rates

    @staticmethod
    def _shock_state(
        *,
        market_state: ImmutableMarketStateLike,
        base_state_hash: str,
        base_marks: Mapping[str, Decimal],
        base_fx: Mapping[str, Decimal],
        shock: JointMarketShock,
        valuation_at: str,
    ) -> ShockedMarketState:
        prices = dict(base_marks)
        for instrument_id, price_return in shock.price_returns:
            if instrument_id not in prices:
                raise ScenarioError(f"scenario_price_target_not_held:{instrument_id}")
            prices[instrument_id] *= _ONE + price_return
        for instrument_id, shift in shock.price_absolute_shifts:
            if instrument_id not in prices:
                raise ScenarioError(f"scenario_price_target_not_held:{instrument_id}")
            prices[instrument_id] += shift
        if any(price <= _ZERO for price in prices.values()):
            raise ScenarioError("scenario_shocked_price_nonpositive")
        fx_rates = dict(base_fx)
        for currency, fx_return in shock.fx_returns:
            if currency not in fx_rates:
                raise ScenarioError(f"scenario_fx_target_not_held:{currency}")
            if currency == market_state.base_currency and fx_return != _ZERO:
                raise ScenarioError("scenario_base_currency_fx_shock_forbidden")
            fx_rates[currency] *= _ONE + fx_return
        return ShockedMarketState(
            parent_state_id=market_state.state_id,
            parent_state_hash=base_state_hash,
            valuation_at=valuation_at,
            base_currency=market_state.base_currency,
            scenario_hash=shock.content_hash,
            prices=tuple(sorted(prices.items())),
            fx_rates=tuple(sorted(fx_rates.items())),
            volatility_shifts=shock.volatility_shifts,
            rate_shifts=shock.rate_shifts,
            liquidity_haircuts=shock.liquidity_haircuts,
            liquidity_cost_multiplier=shock.liquidity_cost_multiplier,
            margin_multiplier=shock.margin_multiplier,
        )


@dataclass(frozen=True, slots=True)
class PathRiskLimits:
    """Explicit risk boundaries evaluated at every stress-path step."""

    maximum_drawdown_fraction: Decimal
    minimum_margin_surplus: Decimal = Decimal("0")
    minimum_liquidity_surplus: Decimal = Decimal("0")
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _decimal(
            self.maximum_drawdown_fraction,
            "path_limits.maximum_drawdown_fraction",
            nonnegative=True,
        )
        if self.maximum_drawdown_fraction > _ONE:
            raise ScenarioError("path_limits_drawdown_fraction_above_one")
        _decimal(self.minimum_margin_surplus, "path_limits.minimum_margin_surplus")
        _decimal(
            self.minimum_liquidity_surplus,
            "path_limits.minimum_liquidity_surplus",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="path_risk_limits"),
        )

    def identity_payload(self) -> dict[str, str]:
        return {
            "maximum_drawdown_fraction": _decimal_text(self.maximum_drawdown_fraction),
            "minimum_margin_surplus": _decimal_text(self.minimum_margin_surplus),
            "minimum_liquidity_surplus": _decimal_text(self.minimum_liquidity_surplus),
        }


@dataclass(frozen=True, slots=True)
class PathShockStep:
    """One immutable incremental shock in an explicitly linked path."""

    sequence: int
    step_id: str
    effective_at: str
    predecessor_hash: str
    shock: JointMarketShock
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
        ):
            raise ScenarioError("path_step_sequence_invalid")
        _require_id(self.step_id, "path_step.step_id")
        object.__setattr__(
            self,
            "effective_at",
            _timestamp_text(self.effective_at, "path_step.effective_at"),
        )
        _require_hash(self.predecessor_hash, "path_step.predecessor_hash")
        if not isinstance(self.shock, JointMarketShock):
            raise ScenarioError("path_step_shock_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="path_shock_step"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "step_id": self.step_id,
            "effective_at": self.effective_at,
            "predecessor_hash": self.predecessor_hash,
            "shock_hash": self.shock.content_hash,
        }


@dataclass(frozen=True, slots=True)
class PathStressScenario:
    """A bounded, chronological and hash-linked sequence of shocks."""

    path_id: str
    expected_base_state_hash: str
    expected_ledger_hash: str
    steps: tuple[PathShockStep, ...]
    risk_limits: PathRiskLimits
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.path_id, "path.path_id")
        _require_hash(self.expected_base_state_hash, "path.expected_base_state_hash")
        _require_hash(self.expected_ledger_hash, "path.expected_ledger_hash")
        steps = tuple(self.steps)
        object.__setattr__(self, "steps", steps)
        if not steps:
            raise ScenarioError("path_steps_required")
        if len(steps) > _HARD_MAX_PATH_STEPS:
            raise ScenarioError("path_steps_exceed_hard_limit")
        if not isinstance(self.risk_limits, PathRiskLimits):
            raise ScenarioError("path_risk_limits_invalid")

        step_ids: set[str] = set()
        shock_ids: set[str] = set()
        predecessor = self.expected_base_state_hash
        prior_time: datetime | None = None
        for expected_sequence, step in enumerate(steps, start=1):
            if not isinstance(step, PathShockStep):
                raise ScenarioError("path_step_invalid")
            if step.sequence != expected_sequence:
                raise ScenarioError("path_step_sequence_gap")
            if step.predecessor_hash != predecessor:
                raise ScenarioError("path_step_hash_chain_broken")
            if step.step_id in step_ids:
                raise ScenarioError("path_step_id_duplicate")
            if step.shock.scenario_id in shock_ids:
                raise ScenarioError("path_shock_scenario_id_duplicate")
            current_time = _timestamp(step.effective_at, "path_step.effective_at")
            if prior_time is not None and current_time <= prior_time:
                raise ScenarioError("path_step_chronology_not_strict")
            step_ids.add(step.step_id)
            shock_ids.add(step.shock.scenario_id)
            predecessor = step.content_hash
            prior_time = current_time

        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="path_stress_scenario"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "path_id": self.path_id,
            "expected_base_state_hash": self.expected_base_state_hash,
            "expected_ledger_hash": self.expected_ledger_hash,
            "step_hashes": [step.content_hash for step in self.steps],
            "risk_limits_hash": self.risk_limits.content_hash,
        }


@dataclass(frozen=True, slots=True)
class PathRiskEvidence:
    """Drawdown, stressed-margin and liquidation-liquidity evidence."""

    limits_hash: str
    peak_nav: Decimal
    current_nav: Decimal
    drawdown_amount: Decimal
    drawdown_fraction: Decimal
    maximum_drawdown_fraction: Decimal
    margin_surplus: Decimal
    minimum_margin_surplus: Decimal
    margin_headroom: Decimal
    liquidity_surplus: Decimal
    minimum_liquidity_surplus: Decimal
    liquidity_headroom: Decimal
    funding_requirement: Decimal
    drawdown_breach: bool
    margin_breach: bool
    liquidity_breach: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.limits_hash, "path_risk_evidence.limits_hash")
        for field_name in (
            "peak_nav",
            "current_nav",
            "drawdown_amount",
            "drawdown_fraction",
            "maximum_drawdown_fraction",
            "margin_surplus",
            "minimum_margin_surplus",
            "margin_headroom",
            "liquidity_surplus",
            "minimum_liquidity_surplus",
            "liquidity_headroom",
            "funding_requirement",
        ):
            _decimal(
                getattr(self, field_name),
                f"path_risk_evidence.{field_name}",
                nonnegative=field_name
                in {
                    "drawdown_amount",
                    "drawdown_fraction",
                    "maximum_drawdown_fraction",
                    "funding_requirement",
                },
            )
        for field_name in (
            "drawdown_breach",
            "margin_breach",
            "liquidity_breach",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ScenarioError(f"path_risk_evidence.{field_name}_invalid")
        if self.peak_nav <= _ZERO:
            raise ScenarioError("path_risk_evidence_peak_nav_nonpositive")
        expected_drawdown = max(self.peak_nav - self.current_nav, _ZERO)
        if self.drawdown_amount != expected_drawdown:
            raise ScenarioError("path_risk_evidence_drawdown_amount_mismatch")
        if self.drawdown_fraction != expected_drawdown / self.peak_nav:
            raise ScenarioError("path_risk_evidence_drawdown_fraction_mismatch")
        if self.margin_headroom != self.margin_surplus - self.minimum_margin_surplus:
            raise ScenarioError("path_risk_evidence_margin_headroom_mismatch")
        if (
            self.liquidity_headroom
            != self.liquidity_surplus - self.minimum_liquidity_surplus
        ):
            raise ScenarioError("path_risk_evidence_liquidity_headroom_mismatch")
        expected_funding = max(
            -self.margin_headroom,
            -self.liquidity_headroom,
            _ZERO,
        )
        if self.funding_requirement != expected_funding:
            raise ScenarioError("path_risk_evidence_funding_requirement_mismatch")
        if self.drawdown_breach != (
            self.drawdown_fraction > self.maximum_drawdown_fraction
        ):
            raise ScenarioError("path_risk_evidence_drawdown_breach_mismatch")
        if self.margin_breach != (self.margin_headroom < _ZERO):
            raise ScenarioError("path_risk_evidence_margin_breach_mismatch")
        if self.liquidity_breach != (self.liquidity_headroom < _ZERO):
            raise ScenarioError("path_risk_evidence_liquidity_breach_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="path_risk_evidence"),
        )

    @property
    def any_breach(self) -> bool:
        return self.drawdown_breach or self.margin_breach or self.liquidity_breach

    def identity_payload(self) -> dict[str, object]:
        return {
            "limits_hash": self.limits_hash,
            "peak_nav": _decimal_text(self.peak_nav),
            "current_nav": _decimal_text(self.current_nav),
            "drawdown_amount": _decimal_text(self.drawdown_amount),
            "drawdown_fraction": _decimal_text(self.drawdown_fraction),
            "maximum_drawdown_fraction": _decimal_text(self.maximum_drawdown_fraction),
            "margin_surplus": _decimal_text(self.margin_surplus),
            "minimum_margin_surplus": _decimal_text(self.minimum_margin_surplus),
            "margin_headroom": _decimal_text(self.margin_headroom),
            "liquidity_surplus": _decimal_text(self.liquidity_surplus),
            "minimum_liquidity_surplus": _decimal_text(self.minimum_liquidity_surplus),
            "liquidity_headroom": _decimal_text(self.liquidity_headroom),
            "funding_requirement": _decimal_text(self.funding_requirement),
            "drawdown_breach": self.drawdown_breach,
            "margin_breach": self.margin_breach,
            "liquidity_breach": self.liquidity_breach,
        }


@dataclass(frozen=True, slots=True)
class PathScenarioStepResult:
    """One cumulative portfolio revaluation and its chain-bound evidence."""

    sequence: int
    step_id: str
    effective_at: str
    definition_step_hash: str
    predecessor_result_hash: str
    prior_state_hash: str
    cumulative_shock_hash: str
    scenario_result: JointScenarioResult
    period_nav_change: Decimal
    cumulative_nav_change: Decimal
    risk_evidence: PathRiskEvidence
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
        ):
            raise ScenarioError("path_step_result_sequence_invalid")
        _require_id(self.step_id, "path_step_result.step_id")
        object.__setattr__(
            self,
            "effective_at",
            _timestamp_text(self.effective_at, "path_step_result.effective_at"),
        )
        for field_name in (
            "definition_step_hash",
            "predecessor_result_hash",
            "prior_state_hash",
            "cumulative_shock_hash",
        ):
            _require_hash(
                getattr(self, field_name),
                f"path_step_result.{field_name}",
            )
        if not isinstance(self.scenario_result, JointScenarioResult):
            raise ScenarioError("path_step_result_scenario_result_invalid")
        if not isinstance(self.risk_evidence, PathRiskEvidence):
            raise ScenarioError("path_step_result_risk_evidence_invalid")
        if self.scenario_result.scenario_hash != self.cumulative_shock_hash:
            raise ScenarioError("path_step_result_cumulative_shock_hash_mismatch")
        if self.scenario_result.shocked_state.valuation_at != self.effective_at:
            raise ScenarioError("path_step_result_effective_time_mismatch")
        if self.risk_evidence.current_nav != self.scenario_result.shocked_valuation.nav:
            raise ScenarioError("path_step_result_risk_nav_mismatch")
        _decimal(self.period_nav_change, "path_step_result.period_nav_change")
        _decimal(
            self.cumulative_nav_change,
            "path_step_result.cumulative_nav_change",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="path_scenario_step_result"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "step_id": self.step_id,
            "effective_at": self.effective_at,
            "definition_step_hash": self.definition_step_hash,
            "predecessor_result_hash": self.predecessor_result_hash,
            "prior_state_hash": self.prior_state_hash,
            "current_state_hash": self.scenario_result.shocked_state_hash,
            "cumulative_shock_hash": self.cumulative_shock_hash,
            "scenario_result_hash": self.scenario_result.content_hash,
            "period_nav_change": _decimal_text(self.period_nav_change),
            "cumulative_nav_change": _decimal_text(self.cumulative_nav_change),
            "risk_evidence_hash": self.risk_evidence.content_hash,
        }


def _path_chain_root(scenario: PathStressScenario) -> str:
    return sha256_prefixed(
        {
            "path_definition_hash": scenario.content_hash,
            "base_state_hash": scenario.expected_base_state_hash,
            "ledger_hash": scenario.expected_ledger_hash,
        },
        label="path_scenario_result_chain_root",
    )


@dataclass(frozen=True, slots=True)
class PathScenarioResult:
    """Verified full-path result with aggregate breach and drawdown evidence."""

    scenario: PathStressScenario
    chain_root_hash: str
    steps: tuple[PathScenarioStepResult, ...]
    maximum_drawdown_amount: Decimal
    maximum_drawdown_fraction: Decimal
    maximum_funding_requirement: Decimal
    worst_margin_headroom: Decimal
    worst_liquidity_headroom: Decimal
    first_drawdown_breach_step_id: str | None
    first_margin_breach_step_id: str | None
    first_liquidity_breach_step_id: str | None
    original_state_unchanged: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, PathStressScenario):
            raise ScenarioError("path_result_scenario_invalid")
        _require_hash(self.chain_root_hash, "path_result.chain_root_hash")
        if self.chain_root_hash != _path_chain_root(self.scenario):
            raise ScenarioError("path_result_chain_root_mismatch")
        steps = tuple(self.steps)
        object.__setattr__(self, "steps", steps)
        if len(steps) != len(self.scenario.steps):
            raise ScenarioError("path_result_step_count_mismatch")
        for field_name in (
            "maximum_drawdown_amount",
            "maximum_drawdown_fraction",
            "maximum_funding_requirement",
            "worst_margin_headroom",
            "worst_liquidity_headroom",
        ):
            _decimal(
                getattr(self, field_name),
                f"path_result.{field_name}",
                nonnegative=field_name
                in {
                    "maximum_drawdown_amount",
                    "maximum_drawdown_fraction",
                    "maximum_funding_requirement",
                },
            )
        if not isinstance(self.original_state_unchanged, bool):
            raise ScenarioError("path_result.original_state_unchanged_invalid")
        for field_name in (
            "first_drawdown_breach_step_id",
            "first_margin_breach_step_id",
            "first_liquidity_breach_step_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_id(value, f"path_result.{field_name}")

        predecessor_result_hash = self.chain_root_hash
        prior_state_hash = self.scenario.expected_base_state_hash
        base_nav = steps[0].scenario_result.base_valuation.nav
        if base_nav <= _ZERO:
            raise ScenarioError("path_result_base_nav_nonpositive")
        prior_nav = base_nav
        peak_nav = base_nav
        drawdown_breach: str | None = None
        margin_breach: str | None = None
        liquidity_breach: str | None = None
        for definition, result in zip(self.scenario.steps, steps, strict=True):
            if result.sequence != definition.sequence:
                raise ScenarioError("path_result_step_sequence_mismatch")
            if result.step_id != definition.step_id:
                raise ScenarioError("path_result_step_id_mismatch")
            if result.effective_at != definition.effective_at:
                raise ScenarioError("path_result_step_time_mismatch")
            if result.definition_step_hash != definition.content_hash:
                raise ScenarioError("path_result_definition_step_hash_mismatch")
            if result.predecessor_result_hash != predecessor_result_hash:
                raise ScenarioError("path_result_hash_chain_broken")
            if result.prior_state_hash != prior_state_hash:
                raise ScenarioError("path_result_state_chain_broken")
            if (
                result.scenario_result.base_state_hash
                != self.scenario.expected_base_state_hash
            ):
                raise ScenarioError("path_result_base_state_hash_mismatch")
            if result.scenario_result.ledger_hash != self.scenario.expected_ledger_hash:
                raise ScenarioError("path_result_ledger_hash_mismatch")
            current_nav = result.scenario_result.shocked_valuation.nav
            if result.period_nav_change != current_nav - prior_nav:
                raise ScenarioError("path_result_period_nav_change_mismatch")
            if result.cumulative_nav_change != current_nav - base_nav:
                raise ScenarioError("path_result_cumulative_nav_change_mismatch")
            peak_nav = max(peak_nav, current_nav)
            evidence = result.risk_evidence
            if evidence.limits_hash != self.scenario.risk_limits.content_hash:
                raise ScenarioError("path_result_risk_limits_hash_mismatch")
            if evidence.peak_nav != peak_nav:
                raise ScenarioError("path_result_peak_nav_mismatch")
            expected_margin_surplus = (
                result.scenario_result.shocked_valuation.available_capital
                + result.scenario_result.liquidity_reserve
            )
            if evidence.margin_surplus != expected_margin_surplus:
                raise ScenarioError("path_result_margin_surplus_mismatch")
            if (
                evidence.liquidity_surplus
                != result.scenario_result.shocked_valuation.available_capital
            ):
                raise ScenarioError("path_result_liquidity_surplus_mismatch")
            if evidence.maximum_drawdown_fraction != (
                self.scenario.risk_limits.maximum_drawdown_fraction
            ):
                raise ScenarioError("path_result_drawdown_limit_mismatch")
            if evidence.minimum_margin_surplus != (
                self.scenario.risk_limits.minimum_margin_surplus
            ):
                raise ScenarioError("path_result_margin_limit_mismatch")
            if evidence.minimum_liquidity_surplus != (
                self.scenario.risk_limits.minimum_liquidity_surplus
            ):
                raise ScenarioError("path_result_liquidity_limit_mismatch")
            if drawdown_breach is None and evidence.drawdown_breach:
                drawdown_breach = result.step_id
            if margin_breach is None and evidence.margin_breach:
                margin_breach = result.step_id
            if liquidity_breach is None and evidence.liquidity_breach:
                liquidity_breach = result.step_id
            predecessor_result_hash = result.content_hash
            prior_state_hash = result.scenario_result.shocked_state_hash
            prior_nav = current_nav

        expected_maximum_drawdown_amount = max(
            item.risk_evidence.drawdown_amount for item in steps
        )
        expected_maximum_drawdown_fraction = max(
            item.risk_evidence.drawdown_fraction for item in steps
        )
        expected_worst_margin_headroom = min(
            item.risk_evidence.margin_headroom for item in steps
        )
        expected_worst_liquidity_headroom = min(
            item.risk_evidence.liquidity_headroom for item in steps
        )
        expected_maximum_funding_requirement = max(
            item.risk_evidence.funding_requirement for item in steps
        )
        expected_values = (
            (self.maximum_drawdown_amount, expected_maximum_drawdown_amount),
            (self.maximum_drawdown_fraction, expected_maximum_drawdown_fraction),
            (
                self.maximum_funding_requirement,
                expected_maximum_funding_requirement,
            ),
            (self.worst_margin_headroom, expected_worst_margin_headroom),
            (self.worst_liquidity_headroom, expected_worst_liquidity_headroom),
        )
        if any(actual != expected for actual, expected in expected_values):
            raise ScenarioError("path_result_aggregate_metric_mismatch")
        if self.first_drawdown_breach_step_id != drawdown_breach:
            raise ScenarioError("path_result_first_drawdown_breach_mismatch")
        if self.first_margin_breach_step_id != margin_breach:
            raise ScenarioError("path_result_first_margin_breach_mismatch")
        if self.first_liquidity_breach_step_id != liquidity_breach:
            raise ScenarioError("path_result_first_liquidity_breach_mismatch")
        if self.original_state_unchanged != all(
            item.scenario_result.original_state_unchanged for item in steps
        ):
            raise ScenarioError("path_result_state_immutability_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="path_scenario_result"),
        )

    @property
    def any_breach(self) -> bool:
        return any(
            item is not None
            for item in (
                self.first_drawdown_breach_step_id,
                self.first_margin_breach_step_id,
                self.first_liquidity_breach_step_id,
            )
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "path_definition_hash": self.scenario.content_hash,
            "chain_root_hash": self.chain_root_hash,
            "step_result_hashes": [item.content_hash for item in self.steps],
            "maximum_drawdown_amount": _decimal_text(self.maximum_drawdown_amount),
            "maximum_drawdown_fraction": _decimal_text(self.maximum_drawdown_fraction),
            "maximum_funding_requirement": _decimal_text(
                self.maximum_funding_requirement
            ),
            "worst_margin_headroom": _decimal_text(self.worst_margin_headroom),
            "worst_liquidity_headroom": _decimal_text(self.worst_liquidity_headroom),
            "first_drawdown_breach_step_id": self.first_drawdown_breach_step_id,
            "first_margin_breach_step_id": self.first_margin_breach_step_id,
            "first_liquidity_breach_step_id": self.first_liquidity_breach_step_id,
            "original_state_unchanged": self.original_state_unchanged,
        }


@dataclass(frozen=True, slots=True)
class PathScenarioEngine:
    """Evaluate a bounded sequence of persistent incremental joint shocks.

    Price and FX changes compound in path order, absolute price shifts apply
    after that step's return, rate/volatility shifts add, and margin/liquidity
    multipliers compound.  Positions and the ledger remain fixed; this is a
    stress revaluation engine, not a trading or forced-liquidation simulator.
    It therefore does not generate margin-transfer events, option hedges,
    futures rolls, early exercise, or liquidation orders.  Those lifecycle
    transitions must be prepared by their product engine as immutable ledger
    snapshots before a path is evaluated.
    """

    joint_engine: JointScenarioEngine = field(default_factory=JointScenarioEngine)
    max_steps: int = 252

    def __post_init__(self) -> None:
        if not isinstance(self.joint_engine, JointScenarioEngine):
            raise ScenarioError("path_engine_joint_engine_invalid")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps <= 0
            or self.max_steps > _HARD_MAX_PATH_STEPS
        ):
            raise ScenarioError("path_engine_max_steps_invalid")

    def evaluate(
        self,
        snapshot: PortfolioSnapshot,
        *,
        market_state: ImmutableMarketStateLike,
        scenario: PathStressScenario,
        repricers: Mapping[str, PositionRepricer] | None = None,
        base_liquidation_costs: Mapping[str, Decimal] | None = None,
    ) -> PathScenarioResult:
        base_state_hash = market_state.state_hash()
        _require_hash(base_state_hash, "path_engine.market_state_hash")
        if base_state_hash != scenario.expected_base_state_hash:
            raise ScenarioError("path_engine_base_state_hash_mismatch")
        if snapshot.ledger_hash != scenario.expected_ledger_hash:
            raise ScenarioError("path_engine_ledger_hash_mismatch")
        if len(scenario.steps) > self.max_steps:
            raise ScenarioError("path_engine_step_limit_exceeded")
        market_valuation_time = _timestamp(
            market_state.valuation_at,
            "path_engine.market_state.valuation_at",
        )
        if (
            _timestamp(
                scenario.steps[0].effective_at,
                "path_engine.first_step.effective_at",
            )
            < market_valuation_time
        ):
            raise ScenarioError("path_engine_step_before_market_state")

        base_marks = self.joint_engine._base_marks(snapshot, market_state)
        base_fx = self.joint_engine._base_fx(snapshot, market_state)
        try:
            base_valuation = snapshot.valuation(fx_rates=base_fx, marks=base_marks)
        except PortfolioAccountingError as exc:
            raise ScenarioError(str(exc)) from exc
        if not base_valuation.reconciled:
            raise ScenarioError("path_engine_base_portfolio_not_reconciled")
        if base_valuation.nav <= _ZERO:
            raise ScenarioError("path_engine_base_nav_nonpositive")

        price_levels = dict(base_marks)
        touched_prices: set[str] = set()
        fx_levels = dict(base_fx)
        touched_fx: set[str] = set()
        volatility_shifts: dict[str, Decimal] = {}
        rate_shifts: dict[str, Decimal] = {}
        liquidity_haircuts: dict[str, Decimal] = {}
        liquidity_cost_multiplier = _ONE
        margin_multiplier = _ONE
        source_hashes: set[str] = set()
        chain_root_hash = _path_chain_root(scenario)
        predecessor_result_hash = chain_root_hash
        prior_state_hash = base_state_hash
        prior_nav = base_valuation.nav
        peak_nav = base_valuation.nav
        results: list[PathScenarioStepResult] = []

        for step in scenario.steps:
            shock = step.shock
            self._apply_price_shock(
                price_levels,
                touched_prices,
                shock,
            )
            self._apply_fx_shock(
                fx_levels,
                touched_fx,
                market_state.base_currency,
                shock,
            )
            self._add_factor_shifts(volatility_shifts, shock.volatility_shifts)
            self._add_factor_shifts(rate_shifts, shock.rate_shifts)
            self._compound_haircuts(liquidity_haircuts, shock.liquidity_haircuts)
            liquidity_cost_multiplier *= shock.liquidity_cost_multiplier
            margin_multiplier *= shock.margin_multiplier
            source_hashes.update(shock.source_hashes)
            source_hashes.add(shock.content_hash)
            cumulative_shock = JointMarketShock(
                scenario_id=(
                    f"{scenario.path_id}.step{step.sequence}.{step.step_id}.cumulative"
                ),
                price_absolute_shifts=tuple(
                    sorted(
                        (
                            instrument_id,
                            price_levels[instrument_id] - base_marks[instrument_id],
                        )
                        for instrument_id in touched_prices
                        if price_levels[instrument_id] != base_marks[instrument_id]
                    )
                ),
                fx_returns=tuple(
                    sorted(
                        (
                            currency,
                            (fx_levels[currency] / base_fx[currency]) - _ONE,
                        )
                        for currency in touched_fx
                        if fx_levels[currency] != base_fx[currency]
                    )
                ),
                volatility_shifts=tuple(sorted(volatility_shifts.items())),
                rate_shifts=tuple(sorted(rate_shifts.items())),
                liquidity_haircuts=tuple(sorted(liquidity_haircuts.items())),
                liquidity_cost_multiplier=liquidity_cost_multiplier,
                margin_multiplier=margin_multiplier,
                source_hashes=tuple(sorted(source_hashes)),
            )
            if market_state.state_hash() != base_state_hash:
                raise ScenarioError("path_engine_market_state_changed_before_step")
            joint_result = self.joint_engine.evaluate(
                snapshot,
                market_state=market_state,
                shock=cumulative_shock,
                repricers=repricers,
                base_liquidation_costs=base_liquidation_costs,
                scenario_valuation_at=step.effective_at,
            )
            current_nav = joint_result.shocked_valuation.nav
            peak_nav = max(peak_nav, current_nav)
            margin_surplus = (
                joint_result.shocked_valuation.available_capital
                + joint_result.liquidity_reserve
            )
            liquidity_surplus = joint_result.shocked_valuation.available_capital
            limits = scenario.risk_limits
            drawdown_amount = max(peak_nav - current_nav, _ZERO)
            drawdown_fraction = drawdown_amount / peak_nav
            evidence = PathRiskEvidence(
                limits_hash=limits.content_hash,
                peak_nav=peak_nav,
                current_nav=current_nav,
                drawdown_amount=drawdown_amount,
                drawdown_fraction=drawdown_fraction,
                maximum_drawdown_fraction=limits.maximum_drawdown_fraction,
                margin_surplus=margin_surplus,
                minimum_margin_surplus=limits.minimum_margin_surplus,
                margin_headroom=margin_surplus - limits.minimum_margin_surplus,
                liquidity_surplus=liquidity_surplus,
                minimum_liquidity_surplus=limits.minimum_liquidity_surplus,
                liquidity_headroom=(
                    liquidity_surplus - limits.minimum_liquidity_surplus
                ),
                funding_requirement=max(
                    limits.minimum_margin_surplus - margin_surplus,
                    limits.minimum_liquidity_surplus - liquidity_surplus,
                    _ZERO,
                ),
                drawdown_breach=(drawdown_fraction > limits.maximum_drawdown_fraction),
                margin_breach=(margin_surplus < limits.minimum_margin_surplus),
                liquidity_breach=(liquidity_surplus < limits.minimum_liquidity_surplus),
            )
            result = PathScenarioStepResult(
                sequence=step.sequence,
                step_id=step.step_id,
                effective_at=step.effective_at,
                definition_step_hash=step.content_hash,
                predecessor_result_hash=predecessor_result_hash,
                prior_state_hash=prior_state_hash,
                cumulative_shock_hash=cumulative_shock.content_hash,
                scenario_result=joint_result,
                period_nav_change=current_nav - prior_nav,
                cumulative_nav_change=current_nav - base_valuation.nav,
                risk_evidence=evidence,
            )
            results.append(result)
            predecessor_result_hash = result.content_hash
            prior_state_hash = joint_result.shocked_state_hash
            prior_nav = current_nav

        if market_state.state_hash() != base_state_hash:
            raise ScenarioError("path_engine_market_state_changed_after_path")
        first_drawdown_breach = next(
            (item.step_id for item in results if item.risk_evidence.drawdown_breach),
            None,
        )
        first_margin_breach = next(
            (item.step_id for item in results if item.risk_evidence.margin_breach),
            None,
        )
        first_liquidity_breach = next(
            (item.step_id for item in results if item.risk_evidence.liquidity_breach),
            None,
        )
        return PathScenarioResult(
            scenario=scenario,
            chain_root_hash=chain_root_hash,
            steps=tuple(results),
            maximum_drawdown_amount=max(
                item.risk_evidence.drawdown_amount for item in results
            ),
            maximum_drawdown_fraction=max(
                item.risk_evidence.drawdown_fraction for item in results
            ),
            maximum_funding_requirement=max(
                item.risk_evidence.funding_requirement for item in results
            ),
            worst_margin_headroom=min(
                item.risk_evidence.margin_headroom for item in results
            ),
            worst_liquidity_headroom=min(
                item.risk_evidence.liquidity_headroom for item in results
            ),
            first_drawdown_breach_step_id=first_drawdown_breach,
            first_margin_breach_step_id=first_margin_breach,
            first_liquidity_breach_step_id=first_liquidity_breach,
            original_state_unchanged=True,
        )

    @staticmethod
    def _apply_price_shock(
        levels: dict[str, Decimal],
        touched: set[str],
        shock: JointMarketShock,
    ) -> None:
        for instrument_id, price_return in shock.price_returns:
            if instrument_id not in levels:
                raise ScenarioError(f"path_price_target_not_held:{instrument_id}")
            levels[instrument_id] *= _ONE + price_return
            touched.add(instrument_id)
        for instrument_id, shift in shock.price_absolute_shifts:
            if instrument_id not in levels:
                raise ScenarioError(f"path_price_target_not_held:{instrument_id}")
            levels[instrument_id] += shift
            touched.add(instrument_id)
        if any(value <= _ZERO for value in levels.values()):
            raise ScenarioError("path_shocked_price_nonpositive")

    @staticmethod
    def _apply_fx_shock(
        levels: dict[str, Decimal],
        touched: set[str],
        base_currency: str,
        shock: JointMarketShock,
    ) -> None:
        for currency, fx_return in shock.fx_returns:
            if currency not in levels:
                raise ScenarioError(f"path_fx_target_not_held:{currency}")
            if currency == base_currency and fx_return != _ZERO:
                raise ScenarioError("path_base_currency_fx_shock_forbidden")
            levels[currency] *= _ONE + fx_return
            touched.add(currency)

    @staticmethod
    def _add_factor_shifts(
        cumulative: dict[str, Decimal],
        increments: tuple[tuple[str, Decimal], ...],
    ) -> None:
        for factor_id, shift in increments:
            cumulative[factor_id] = cumulative.get(factor_id, _ZERO) + shift

    @staticmethod
    def _compound_haircuts(
        cumulative: dict[str, Decimal],
        increments: tuple[tuple[str, Decimal], ...],
    ) -> None:
        for instrument_id, haircut in increments:
            prior = cumulative.get(instrument_id, _ZERO)
            cumulative[instrument_id] = _ONE - ((_ONE - prior) * (_ONE - haircut))
