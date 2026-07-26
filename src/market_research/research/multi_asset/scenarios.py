"""Joint, immutable market shocks and portfolio repricing.

The engine consumes the shared market-state contract structurally.  It never
modifies that state: a shock produces a separate hash-bound view whose parent
hash is retained in the scenario evidence.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Mapping, Protocol, runtime_checkable

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


def _normalize_text_pairs(
    values: tuple[tuple[str, str], ...],
    field_name: str,
) -> tuple[tuple[str, str], ...]:
    normalized = tuple(sorted(values))
    if len({key for key, _ in normalized}) != len(normalized):
        raise ScenarioError(f"{field_name}_duplicate")
    for key, value in normalized:
        _require_id(key, f"{field_name}.key")
        _require_id(value, f"{field_name}.value")
    return normalized


def _component_hash(component: object, *, label: str) -> str:
    """Bind a projected component to its exact immutable source payload."""

    existing = getattr(component, "content_hash", None)
    if isinstance(existing, str):
        _require_hash(existing, f"scenario.{label}.content_hash")
        return existing
    as_dict = getattr(component, "as_dict", None)
    if callable(as_dict):
        return sha256_prefixed(as_dict(), label=f"scenario_source_{label}")
    raise ScenarioError(f"scenario_source_component_not_hashable:{label}")


def _objects(value: object, field_name: str) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ScenarioError(f"scenario_market_state_{field_name}_invalid")
    return tuple(value)


def _attribute_id(value: object, name: str, field_name: str) -> str:
    identifier = getattr(value, name, None)
    if not isinstance(identifier, str):
        raise ScenarioError(f"{field_name}_invalid")
    _require_id(identifier, field_name)
    return identifier


def _attribute_text(value: object, name: str, field_name: str) -> str:
    text = getattr(value, name, None)
    if not isinstance(text, str):
        raise ScenarioError(f"{field_name}_invalid")
    return text


def _attribute_decimal(
    value: object,
    name: str,
    field_name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    number = getattr(value, name, None)
    if not isinstance(number, Decimal):
        raise ScenarioError(f"{field_name}_must_be_decimal")
    return _decimal(
        number,
        field_name,
        nonnegative=nonnegative,
        positive=positive,
    )


@dataclass(frozen=True, slots=True)
class VolatilityPointProjection:
    """One source-bound volatility point projected by a joint scenario."""

    surface_id: str
    underlying_instrument_id: str
    expiry_at: str
    strike: Decimal
    base_volatility: Decimal
    projected_volatility: Decimal
    source_component_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.surface_id, "volatility_projection.surface_id")
        _require_id(
            self.underlying_instrument_id,
            "volatility_projection.underlying_instrument_id",
        )
        object.__setattr__(
            self,
            "expiry_at",
            _timestamp_text(
                self.expiry_at,
                "volatility_projection.expiry_at",
            ),
        )
        _decimal(self.strike, "volatility_projection.strike", positive=True)
        _decimal(
            self.base_volatility,
            "volatility_projection.base_volatility",
            nonnegative=True,
        )
        _decimal(
            self.projected_volatility,
            "volatility_projection.projected_volatility",
            nonnegative=True,
        )
        _require_hash(
            self.source_component_hash,
            "volatility_projection.source_component_hash",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="scenario_volatility_point_projection",
            ),
        )

    def identity_payload(self) -> dict[str, str]:
        return {
            "surface_id": self.surface_id,
            "underlying_instrument_id": self.underlying_instrument_id,
            "expiry_at": self.expiry_at,
            "strike": _decimal_text(self.strike),
            "base_volatility": _decimal_text(self.base_volatility),
            "projected_volatility": _decimal_text(self.projected_volatility),
            "source_component_hash": self.source_component_hash,
        }


@dataclass(frozen=True, slots=True)
class CommonMarketProjection:
    """Hash-bound common-state adapter consumed by every product repricer.

    The adapter inventories prices and economic factors from the actual shared
    market state.  Ledger marks are retained only as an explicit fallback for
    products absent from that state.  This makes an unheld derivative
    underlying available to an option repricer without mutating the source.
    """

    parent_state_id: str
    parent_state_hash: str
    prices: tuple[tuple[str, Decimal], ...]
    price_source_kinds: tuple[tuple[str, str], ...]
    rate_levels: tuple[tuple[str, Decimal], ...]
    rate_target_members: tuple[tuple[str, tuple[str, ...]], ...]
    dividend_yields: tuple[tuple[str, Decimal], ...]
    borrow_rates: tuple[tuple[str, Decimal], ...]
    funding_rates: tuple[tuple[str, Decimal], ...]
    spreads: tuple[tuple[str, Decimal], ...]
    futures_curve_members: tuple[tuple[str, tuple[str, ...]], ...]
    futures_underlyings: tuple[tuple[str, str], ...]
    futures_basis_levels: tuple[tuple[str, Decimal], ...]
    option_underlyings: tuple[tuple[str, str], ...]
    volatility_points: tuple[VolatilityPointProjection, ...]
    source_component_hashes: tuple[tuple[str, str], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.parent_state_id, "common_projection.parent_state_id")
        _require_hash(
            self.parent_state_hash,
            "common_projection.parent_state_hash",
        )
        object.__setattr__(
            self,
            "prices",
            _normalize_pairs(
                self.prices,
                "common_projection.prices",
                positive=True,
            ),
        )
        for field_name in (
            "rate_levels",
            "dividend_yields",
            "borrow_rates",
            "funding_rates",
            "spreads",
            "futures_basis_levels",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_pairs(
                    getattr(self, field_name),
                    f"common_projection.{field_name}",
                    nonnegative=field_name in {"borrow_rates", "spreads"},
                ),
            )
        object.__setattr__(
            self,
            "price_source_kinds",
            _normalize_text_pairs(
                self.price_source_kinds,
                "common_projection.price_source_kinds",
            ),
        )
        if set(dict(self.price_source_kinds)) != set(dict(self.prices)):
            raise ScenarioError("common_projection_price_source_set_mismatch")
        object.__setattr__(
            self,
            "futures_underlyings",
            _normalize_text_pairs(
                self.futures_underlyings,
                "common_projection.futures_underlyings",
            ),
        )
        object.__setattr__(
            self,
            "option_underlyings",
            _normalize_text_pairs(
                self.option_underlyings,
                "common_projection.option_underlyings",
            ),
        )
        for field_name in ("rate_target_members", "futures_curve_members"):
            members = tuple(
                sorted(
                    (target, tuple(sorted(values)))
                    for target, values in getattr(self, field_name)
                )
            )
            if len({target for target, _ in members}) != len(members):
                raise ScenarioError(f"common_projection.{field_name}_duplicate")
            for target, values in members:
                _require_id(target, f"common_projection.{field_name}.target")
                if not values or len(set(values)) != len(values):
                    raise ScenarioError(
                        f"common_projection.{field_name}.members_invalid"
                    )
                for value in values:
                    _require_id(value, f"common_projection.{field_name}.member")
            object.__setattr__(self, field_name, members)
        points = tuple(
            sorted(
                self.volatility_points,
                key=lambda item: (item.surface_id, item.expiry_at, item.strike),
            )
        )
        if any(not isinstance(item, VolatilityPointProjection) for item in points):
            raise ScenarioError("common_projection_volatility_point_invalid")
        point_keys = [(item.surface_id, item.expiry_at, item.strike) for item in points]
        if len(set(point_keys)) != len(point_keys):
            raise ScenarioError("common_projection_volatility_point_duplicate")
        object.__setattr__(self, "volatility_points", points)
        sources = tuple(sorted(self.source_component_hashes))
        if len({key for key, _ in sources}) != len(sources):
            raise ScenarioError("common_projection_source_component_duplicate")
        for key, source_hash in sources:
            _require_id(key, "common_projection.source_component.key")
            _require_hash(
                source_hash,
                "common_projection.source_component.hash",
            )
        object.__setattr__(self, "source_component_hashes", sources)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="scenario_common_market_projection",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        def pairs(values: tuple[tuple[str, Decimal], ...]) -> list[dict[str, str]]:
            return [
                {"key": key, "value": _decimal_text(value)} for key, value in values
            ]

        return {
            "parent_state_id": self.parent_state_id,
            "parent_state_hash": self.parent_state_hash,
            "prices": pairs(self.prices),
            "price_source_kinds": [
                {"instrument_id": key, "source_kind": value}
                for key, value in self.price_source_kinds
            ],
            "rate_levels": pairs(self.rate_levels),
            "rate_target_members": [
                {"target": target, "members": list(members)}
                for target, members in self.rate_target_members
            ],
            "dividend_yields": pairs(self.dividend_yields),
            "borrow_rates": pairs(self.borrow_rates),
            "funding_rates": pairs(self.funding_rates),
            "spreads": pairs(self.spreads),
            "futures_curve_members": [
                {"curve_id": curve_id, "contracts": list(contracts)}
                for curve_id, contracts in self.futures_curve_members
            ],
            "futures_underlyings": [
                {"contract_id": key, "underlying_instrument_id": value}
                for key, value in self.futures_underlyings
            ],
            "futures_basis_levels": pairs(self.futures_basis_levels),
            "option_underlyings": [
                {"contract_id": key, "underlying_instrument_id": value}
                for key, value in self.option_underlyings
            ],
            "volatility_points": [
                item.identity_payload() for item in self.volatility_points
            ],
            "source_component_hashes": [
                {"component": key, "hash": value}
                for key, value in self.source_component_hashes
            ],
        }


@runtime_checkable
class SpotQuoteLike(Protocol):
    @property
    def price(self) -> Decimal: ...


@runtime_checkable
class ImmutableMarketStateLike(Protocol):
    """Narrow structural boundary implemented by ``multi_asset.MarketState``."""

    @property
    def state_id(self) -> str: ...

    @property
    def valuation_at(self) -> str: ...

    @property
    def base_currency(self) -> str: ...

    def state_hash(self) -> str: ...

    def spot_price(self, instrument_id: str) -> SpotQuoteLike: ...

    def convert(
        self, amount: Decimal, *, from_currency: str, to_currency: str
    ) -> Decimal: ...


def build_common_market_projection(
    market_state: ImmutableMarketStateLike,
    *,
    fallback_marks: Mapping[str, Decimal],
) -> CommonMarketProjection:
    """Project every price/factor exposed by the immutable shared state."""

    parent_hash = market_state.state_hash()
    _require_hash(parent_hash, "scenario.market_state_hash")
    prices: dict[str, Decimal] = {}
    price_kinds: dict[str, str] = {}
    sources: dict[str, str] = {}

    def add_source(kind: str, identifier: str, component: object) -> str:
        source_key = f"{kind}:{identifier}"
        source_hash = _component_hash(component, label=kind)
        prior = sources.get(source_key)
        if prior is not None and prior != source_hash:
            raise ScenarioError(f"scenario_source_component_duplicate:{source_key}")
        sources[source_key] = source_hash
        return source_hash

    def add_price(
        instrument_id: str,
        price: Decimal,
        source_kind: str,
    ) -> None:
        _require_id(instrument_id, "scenario.market_price.instrument_id")
        _decimal(price, "scenario.market_price.price", positive=True)
        if instrument_id in prices:
            raise ScenarioError(
                f"scenario_market_price_target_duplicate:{instrument_id}"
            )
        prices[instrument_id] = price
        price_kinds[instrument_id] = source_kind

    spots = _objects(getattr(market_state, "spots", ()), "spots")
    for spot in spots:
        instrument_id = _attribute_id(
            spot,
            "instrument_id",
            "scenario.spot.instrument_id",
        )
        add_source("spot", instrument_id, spot)
        add_price(
            instrument_id,
            _attribute_decimal(
                spot,
                "price",
                "scenario.spot.price",
                positive=True,
            ),
            "market_state_spot",
        )

    rate_levels: dict[str, Decimal] = {}
    rate_members: dict[str, tuple[str, ...]] = {}
    for rate in _objects(getattr(market_state, "rates", ()), "rates"):
        rate_id = _attribute_id(rate, "rate_id", "scenario.rate.rate_id")
        add_source("rate", rate_id, rate)
        rate_levels[rate_id] = _attribute_decimal(
            rate,
            "rate",
            "scenario.rate.rate",
        )
        rate_members[rate_id] = (rate_id,)
    for curve in _objects(getattr(market_state, "curves", ()), "curves"):
        curve_id = _attribute_id(curve, "curve_id", "scenario.curve.curve_id")
        add_source("yield_curve", curve_id, curve)
        member_ids: list[str] = []
        for point in _objects(getattr(curve, "points", ()), "curve_points"):
            tenor_days = getattr(point, "tenor_days", None)
            if (
                isinstance(tenor_days, bool)
                or not isinstance(tenor_days, int)
                or tenor_days <= 0
            ):
                raise ScenarioError("scenario_curve_tenor_invalid")
            member_id = f"{curve_id}@{tenor_days}d"
            member_ids.append(member_id)
            rate_levels[member_id] = _attribute_decimal(
                point,
                "rate",
                "scenario.curve_point.rate",
            )
        if not member_ids:
            raise ScenarioError(f"scenario_curve_points_missing:{curve_id}")
        rate_members[curve_id] = tuple(sorted(member_ids))

    dividend_yields: dict[str, Decimal] = {}
    for assumption in _objects(
        getattr(market_state, "dividend_yields", ()),
        "dividend_yields",
    ):
        underlying_id = _attribute_id(
            assumption,
            "underlying_instrument_id",
            "scenario.dividend_yield.underlying_instrument_id",
        )
        assumption_id = _attribute_id(
            assumption,
            "assumption_id",
            "scenario.dividend_yield.assumption_id",
        )
        add_source("dividend_yield", assumption_id, assumption)
        dividend_yields[underlying_id] = _attribute_decimal(
            assumption,
            "annualized_yield",
            "scenario.dividend_yield.annualized_yield",
        )

    borrow_rates: dict[str, Decimal] = {}
    for borrow in _objects(
        getattr(market_state, "borrow_quotes", ()),
        "borrow_quotes",
    ):
        instrument_id = _attribute_id(
            borrow,
            "instrument_id",
            "scenario.borrow.instrument_id",
        )
        add_source("borrow", instrument_id, borrow)
        borrow_rates[instrument_id] = _attribute_decimal(
            borrow,
            "annualized_rate",
            "scenario.borrow.annualized_rate",
            nonnegative=True,
        )

    funding_rates: dict[str, Decimal] = {}
    for funding in _objects(
        getattr(market_state, "funding_rates", ()),
        "funding_rates",
    ):
        funding_id = _attribute_id(
            funding,
            "funding_rate_id",
            "scenario.funding_rate.funding_rate_id",
        )
        add_source("funding_rate", funding_id, funding)
        funding_rates[funding_id] = _attribute_decimal(
            funding,
            "annualized_rate",
            "scenario.funding_rate.annualized_rate",
        )

    spreads: dict[str, Decimal] = {}
    futures_members: dict[str, tuple[str, ...]] = {}
    futures_underlyings: dict[str, str] = {}
    for curve in _objects(
        getattr(market_state, "futures_curves", ()),
        "futures_curves",
    ):
        curve_id = _attribute_id(
            curve,
            "curve_id",
            "scenario.futures_curve.curve_id",
        )
        add_source("futures_curve", curve_id, curve)
        contract_ids: list[str] = []
        for contract in _objects(
            getattr(curve, "contracts", ()),
            "futures_contracts",
        ):
            contract_id = _attribute_id(
                contract,
                "contract_id",
                "scenario.futures_contract.contract_id",
            )
            contract_ids.append(contract_id)
            futures_underlyings[contract_id] = _attribute_id(
                contract,
                "underlying_instrument_id",
                "scenario.futures_contract.underlying_instrument_id",
            )
            add_price(
                contract_id,
                _attribute_decimal(
                    contract,
                    "mark_price",
                    "scenario.futures_contract.mark_price",
                    positive=True,
                ),
                "market_state_futures_contract",
            )
            bid = _attribute_decimal(
                contract,
                "bid",
                "scenario.futures_contract.bid",
                positive=True,
            )
            ask = _attribute_decimal(
                contract,
                "ask",
                "scenario.futures_contract.ask",
                positive=True,
            )
            spreads[contract_id] = ask - bid
        if not contract_ids:
            raise ScenarioError(f"scenario_futures_curve_contracts_missing:{curve_id}")
        futures_members[curve_id] = tuple(sorted(contract_ids))

    option_underlyings: dict[str, str] = {}
    for chain in _objects(
        getattr(market_state, "option_chains", ()),
        "option_chains",
    ):
        chain_id = _attribute_id(
            chain,
            "chain_id",
            "scenario.option_chain.chain_id",
        )
        add_source("option_chain", chain_id, chain)
        chain_underlying_id = _attribute_id(
            chain,
            "underlying_instrument_id",
            "scenario.option_chain.underlying_instrument_id",
        )
        analytics = {
            _attribute_id(
                item,
                "contract_id",
                "scenario.option_analytics.contract_id",
            ): item
            for item in _objects(
                getattr(chain, "analytics", ()),
                "option_analytics",
            )
        }
        for quote in _objects(
            getattr(chain, "quotes", ()),
            "option_quotes",
        ):
            contract_id = _attribute_id(
                quote,
                "contract_id",
                "scenario.option_quote.contract_id",
            )
            mark = analytics.get(contract_id)
            if mark is None:
                raise ScenarioError(f"scenario_option_analytics_missing:{contract_id}")
            option_underlyings[contract_id] = chain_underlying_id
            add_price(
                contract_id,
                _attribute_decimal(
                    mark,
                    "market_price",
                    "scenario.option_analytics.market_price",
                    positive=True,
                ),
                "market_state_option_analytics",
            )
            bid = _attribute_decimal(
                quote,
                "bid",
                "scenario.option_quote.bid",
                positive=True,
            )
            ask = _attribute_decimal(
                quote,
                "ask",
                "scenario.option_quote.ask",
                positive=True,
            )
            spreads[contract_id] = ask - bid

    for liquidity in _objects(
        getattr(market_state, "liquidity_quotes", ()),
        "liquidity_quotes",
    ):
        instrument_id = _attribute_id(
            liquidity,
            "instrument_id",
            "scenario.liquidity.instrument_id",
        )
        add_source("liquidity", instrument_id, liquidity)
        bid = _attribute_decimal(
            liquidity,
            "bid",
            "scenario.liquidity.bid",
            positive=True,
        )
        ask = _attribute_decimal(
            liquidity,
            "ask",
            "scenario.liquidity.ask",
            positive=True,
        )
        spreads[instrument_id] = ask - bid

    volatility_points: list[VolatilityPointProjection] = []
    for surface in _objects(
        getattr(market_state, "volatility_surfaces", ()),
        "volatility_surfaces",
    ):
        surface_id = _attribute_id(
            surface,
            "surface_id",
            "scenario.volatility_surface.surface_id",
        )
        underlying_id = _attribute_id(
            surface,
            "underlying_instrument_id",
            "scenario.volatility_surface.underlying_instrument_id",
        )
        source_hash = add_source("volatility_surface", surface_id, surface)
        for point in _objects(
            getattr(surface, "points", ()),
            "volatility_points",
        ):
            volatility = _attribute_decimal(
                point,
                "volatility",
                "scenario.volatility_point.volatility",
                nonnegative=True,
            )
            volatility_points.append(
                VolatilityPointProjection(
                    surface_id=surface_id,
                    underlying_instrument_id=underlying_id,
                    expiry_at=_attribute_text(
                        point,
                        "expiry_at",
                        "scenario.volatility_point.expiry_at",
                    ),
                    strike=_attribute_decimal(
                        point,
                        "strike",
                        "scenario.volatility_point.strike",
                        positive=True,
                    ),
                    base_volatility=volatility,
                    projected_volatility=volatility,
                    source_component_hash=source_hash,
                )
            )

    for instrument_id, mark in fallback_marks.items():
        if instrument_id in prices:
            continue
        add_price(instrument_id, mark, "ledger_mark_fallback")

    futures_basis_levels: dict[str, Decimal] = {}
    for contract_id, underlying_id in futures_underlyings.items():
        try:
            futures_basis_levels[contract_id] = (
                prices[contract_id] - prices[underlying_id]
            )
        except KeyError as exc:
            raise ScenarioError(
                f"scenario_futures_underlying_price_missing:{contract_id}"
            ) from exc

    return CommonMarketProjection(
        parent_state_id=market_state.state_id,
        parent_state_hash=parent_hash,
        prices=tuple(prices.items()),
        price_source_kinds=tuple(price_kinds.items()),
        rate_levels=tuple(rate_levels.items()),
        rate_target_members=tuple(rate_members.items()),
        dividend_yields=tuple(dividend_yields.items()),
        borrow_rates=tuple(borrow_rates.items()),
        funding_rates=tuple(funding_rates.items()),
        spreads=tuple(spreads.items()),
        futures_curve_members=tuple(futures_members.items()),
        futures_underlyings=tuple(futures_underlyings.items()),
        futures_basis_levels=tuple(futures_basis_levels.items()),
        option_underlyings=tuple(option_underlyings.items()),
        volatility_points=tuple(volatility_points),
        source_component_hashes=tuple(sources.items()),
    )


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
    volatility_skew_shifts: tuple[tuple[str, Decimal], ...] = ()
    volatility_term_shifts: tuple[tuple[str, Decimal], ...] = ()
    rate_shifts: tuple[tuple[str, Decimal], ...] = ()
    dividend_yield_shifts: tuple[tuple[str, Decimal], ...] = ()
    borrow_rate_shifts: tuple[tuple[str, Decimal], ...] = ()
    funding_rate_shifts: tuple[tuple[str, Decimal], ...] = ()
    futures_curve_returns: tuple[tuple[str, Decimal], ...] = ()
    futures_basis_shifts: tuple[tuple[str, Decimal], ...] = ()
    spread_multipliers: tuple[tuple[str, Decimal], ...] = ()
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
            "volatility_skew_shifts",
            "volatility_term_shifts",
            "rate_shifts",
            "dividend_yield_shifts",
            "borrow_rate_shifts",
            "funding_rate_shifts",
            "futures_curve_returns",
            "futures_basis_shifts",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_pairs(getattr(self, field_name), f"scenario.{field_name}"),
            )
        object.__setattr__(
            self,
            "spread_multipliers",
            _normalize_pairs(
                self.spread_multipliers,
                "scenario.spread_multipliers",
                positive=True,
            ),
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
        if any(value <= -_ONE for _, value in self.futures_curve_returns):
            raise ScenarioError("scenario_futures_curve_return_at_or_below_minus_one")
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
            "volatility_skew_shifts": pairs(self.volatility_skew_shifts),
            "volatility_term_shifts": pairs(self.volatility_term_shifts),
            "rate_shifts": pairs(self.rate_shifts),
            "dividend_yield_shifts": pairs(self.dividend_yield_shifts),
            "borrow_rate_shifts": pairs(self.borrow_rate_shifts),
            "funding_rate_shifts": pairs(self.funding_rate_shifts),
            "futures_curve_returns": pairs(self.futures_curve_returns),
            "futures_basis_shifts": pairs(self.futures_basis_shifts),
            "spread_multipliers": pairs(self.spread_multipliers),
            "liquidity_haircuts": pairs(self.liquidity_haircuts),
            "liquidity_cost_multiplier": _decimal_text(self.liquidity_cost_multiplier),
            "margin_multiplier": _decimal_text(self.margin_multiplier),
            "source_hashes": list(self.source_hashes),
        }


@dataclass(frozen=True, slots=True)
class ShockedMarketState:
    """Immutable full-source projection containing scenario-adjusted factors."""

    parent_state_id: str
    parent_state_hash: str
    base_projection_hash: str
    valuation_at: str
    base_currency: str
    scenario_hash: str
    prices: tuple[tuple[str, Decimal], ...]
    price_source_kinds: tuple[tuple[str, str], ...]
    fx_rates: tuple[tuple[str, Decimal], ...]
    rate_levels: tuple[tuple[str, Decimal], ...]
    dividend_yields: tuple[tuple[str, Decimal], ...]
    borrow_rates: tuple[tuple[str, Decimal], ...]
    funding_rates: tuple[tuple[str, Decimal], ...]
    spreads: tuple[tuple[str, Decimal], ...]
    futures_basis_levels: tuple[tuple[str, Decimal], ...]
    volatility_points: tuple[VolatilityPointProjection, ...]
    volatility_shifts: tuple[tuple[str, Decimal], ...]
    volatility_skew_shifts: tuple[tuple[str, Decimal], ...]
    volatility_term_shifts: tuple[tuple[str, Decimal], ...]
    rate_shifts: tuple[tuple[str, Decimal], ...]
    dividend_yield_shifts: tuple[tuple[str, Decimal], ...]
    borrow_rate_shifts: tuple[tuple[str, Decimal], ...]
    funding_rate_shifts: tuple[tuple[str, Decimal], ...]
    futures_curve_returns: tuple[tuple[str, Decimal], ...]
    futures_basis_shifts: tuple[tuple[str, Decimal], ...]
    spread_multipliers: tuple[tuple[str, Decimal], ...]
    liquidity_haircuts: tuple[tuple[str, Decimal], ...]
    liquidity_cost_multiplier: Decimal
    margin_multiplier: Decimal
    source_component_hashes: tuple[tuple[str, str], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.parent_state_id, "shocked_state.parent_state_id")
        _require_hash(self.parent_state_hash, "shocked_state.parent_state_hash")
        _require_hash(
            self.base_projection_hash,
            "shocked_state.base_projection_hash",
        )
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
            "price_source_kinds",
            _normalize_text_pairs(
                self.price_source_kinds,
                "shocked_state.price_source_kinds",
            ),
        )
        if set(dict(self.price_source_kinds)) != set(dict(self.prices)):
            raise ScenarioError("shocked_state_price_source_set_mismatch")
        for field_name in (
            "fx_rates",
            "rate_levels",
            "dividend_yields",
            "borrow_rates",
            "funding_rates",
            "spreads",
            "futures_basis_levels",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_pairs(
                    getattr(self, field_name),
                    f"shocked_state.{field_name}",
                    positive=field_name == "fx_rates",
                    nonnegative=field_name in {"borrow_rates", "spreads"},
                ),
            )
        points = tuple(
            sorted(
                self.volatility_points,
                key=lambda item: (item.surface_id, item.expiry_at, item.strike),
            )
        )
        if any(not isinstance(item, VolatilityPointProjection) for item in points):
            raise ScenarioError("shocked_state_volatility_point_invalid")
        object.__setattr__(self, "volatility_points", points)
        for field_name in (
            "volatility_shifts",
            "volatility_skew_shifts",
            "volatility_term_shifts",
            "rate_shifts",
            "dividend_yield_shifts",
            "borrow_rate_shifts",
            "funding_rate_shifts",
            "futures_curve_returns",
            "futures_basis_shifts",
            "spread_multipliers",
            "liquidity_haircuts",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_pairs(
                    getattr(self, field_name),
                    f"shocked_state.{field_name}",
                    positive=field_name == "spread_multipliers",
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
        sources = tuple(sorted(self.source_component_hashes))
        if len({key for key, _ in sources}) != len(sources):
            raise ScenarioError("shocked_state_source_component_duplicate")
        for key, source_hash in sources:
            _require_id(key, "shocked_state.source_component.key")
            _require_hash(source_hash, "shocked_state.source_component.hash")
        object.__setattr__(self, "source_component_hashes", sources)
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

    def factor_value(self, factor_type: str, factor_id: str) -> Decimal:
        """Return one projected economic factor with fail-closed typing."""

        factors: dict[str, tuple[tuple[str, Decimal], ...]] = {
            "rate": self.rate_levels,
            "dividend_yield": self.dividend_yields,
            "borrow_rate": self.borrow_rates,
            "funding_rate": self.funding_rates,
            "spread": self.spreads,
            "futures_basis": self.futures_basis_levels,
        }
        try:
            values = factors[factor_type]
        except KeyError as exc:
            raise ScenarioError(f"scenario_factor_type_unknown:{factor_type}") from exc
        try:
            return dict(values)[factor_id]
        except KeyError as exc:
            raise ScenarioError(
                f"scenario_factor_missing:{factor_type}:{factor_id}"
            ) from exc

    def volatility_for(
        self,
        *,
        surface_id: str,
        expiry_at: str,
        strike: Decimal,
    ) -> Decimal:
        expiry = _timestamp_text(expiry_at, "scenario.volatility.expiry_at")
        matches = [
            item.projected_volatility
            for item in self.volatility_points
            if item.surface_id == surface_id
            and item.expiry_at == expiry
            and item.strike == strike
        ]
        if len(matches) != 1:
            raise ScenarioError(
                "scenario_volatility_point_not_unique:"
                f"{surface_id}:{expiry}:{_decimal_text(strike)}"
            )
        return matches[0]

    def identity_payload(self) -> dict[str, object]:
        def pairs(values: tuple[tuple[str, Decimal], ...]) -> list[dict[str, str]]:
            return [
                {"key": key, "value": _decimal_text(value)} for key, value in values
            ]

        return {
            "parent_state_id": self.parent_state_id,
            "parent_state_hash": self.parent_state_hash,
            "base_projection_hash": self.base_projection_hash,
            "valuation_at": self.valuation_at,
            "base_currency": self.base_currency,
            "scenario_hash": self.scenario_hash,
            "prices": pairs(self.prices),
            "price_source_kinds": [
                {"instrument_id": key, "source_kind": value}
                for key, value in self.price_source_kinds
            ],
            "fx_rates": pairs(self.fx_rates),
            "rate_levels": pairs(self.rate_levels),
            "dividend_yields": pairs(self.dividend_yields),
            "borrow_rates": pairs(self.borrow_rates),
            "funding_rates": pairs(self.funding_rates),
            "spreads": pairs(self.spreads),
            "futures_basis_levels": pairs(self.futures_basis_levels),
            "volatility_points": [
                item.identity_payload() for item in self.volatility_points
            ],
            "volatility_shifts": pairs(self.volatility_shifts),
            "volatility_skew_shifts": pairs(self.volatility_skew_shifts),
            "volatility_term_shifts": pairs(self.volatility_term_shifts),
            "rate_shifts": pairs(self.rate_shifts),
            "dividend_yield_shifts": pairs(self.dividend_yield_shifts),
            "borrow_rate_shifts": pairs(self.borrow_rate_shifts),
            "funding_rate_shifts": pairs(self.funding_rate_shifts),
            "futures_curve_returns": pairs(self.futures_curve_returns),
            "futures_basis_shifts": pairs(self.futures_basis_shifts),
            "spread_multipliers": pairs(self.spread_multipliers),
            "liquidity_haircuts": pairs(self.liquidity_haircuts),
            "liquidity_cost_multiplier": _decimal_text(self.liquidity_cost_multiplier),
            "margin_multiplier": _decimal_text(self.margin_multiplier),
            "source_component_hashes": [
                {"component": key, "hash": value}
                for key, value in self.source_component_hashes
            ],
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

        fallback_marks = {
            position.instrument_id: position.mark_price
            for position in snapshot.positions
        }
        base_projection = build_common_market_projection(
            market_state,
            fallback_marks=fallback_marks,
        )
        if base_projection.parent_state_hash != base_state_hash:
            raise ScenarioError("scenario_common_projection_parent_hash_mismatch")
        base_marks = self._base_marks(
            snapshot,
            market_state,
            projection=base_projection,
        )
        base_fx = self._base_fx(snapshot, market_state)
        shocked_state = self._shock_state(
            market_state=market_state,
            base_state_hash=base_state_hash,
            base_projection=base_projection,
            base_fx=base_fx,
            shock=shock,
            valuation_at=effective_valuation_at,
        )
        shocked_marks = dict(shocked_state.prices)
        return_shocks = set(dict(shock.price_returns))
        absolute_shocks = set(dict(shock.price_absolute_shifts))
        factor_shock_present = bool(
            shock.volatility_shifts
            or shock.volatility_skew_shifts
            or shock.volatility_term_shifts
            or shock.rate_shifts
            or shock.dividend_yield_shifts
            or shock.borrow_rate_shifts
            or shock.funding_rate_shifts
            or shock.futures_curve_returns
            or shock.futures_basis_shifts
            or shock.spread_multipliers
        )
        option_underlyings = dict(base_projection.option_underlyings)
        shocked_price_targets = (
            return_shocks
            | absolute_shocks
            | {
                contract_id
                for curve_id, _ in shock.futures_curve_returns
                for contract_id in dict(base_projection.futures_curve_members).get(
                    curve_id,
                    (),
                )
            }
            | set(dict(shock.futures_basis_shifts))
        )
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
                and position.instrument_id not in return_shocks
                and position.instrument_id not in absolute_shocks
                and (
                    factor_shock_present
                    or option_underlyings.get(position.instrument_id)
                    in shocked_price_targets
                )
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
        *,
        projection: CommonMarketProjection | None = None,
    ) -> dict[str, Decimal]:
        if projection is None:
            projection = build_common_market_projection(
                market_state,
                fallback_marks={
                    position.instrument_id: position.mark_price
                    for position in snapshot.positions
                },
            )
        projected_prices = dict(projection.prices)
        marks: dict[str, Decimal] = {}
        for position in snapshot.positions:
            mark = projected_prices.get(position.instrument_id, position.mark_price)
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
        base_projection: CommonMarketProjection,
        base_fx: Mapping[str, Decimal],
        shock: JointMarketShock,
        valuation_at: str,
    ) -> ShockedMarketState:
        prices = dict(base_projection.prices)
        curve_members = dict(base_projection.futures_curve_members)
        for curve_id, curve_return in shock.futures_curve_returns:
            contracts = curve_members.get(curve_id)
            if contracts is None:
                raise ScenarioError(f"scenario_futures_curve_target_unknown:{curve_id}")
            for contract_id in contracts:
                prices[contract_id] *= _ONE + curve_return
        futures_underlyings = dict(base_projection.futures_underlyings)
        for contract_id, basis_shift in shock.futures_basis_shifts:
            if contract_id not in futures_underlyings:
                raise ScenarioError(
                    f"scenario_futures_basis_target_unknown:{contract_id}"
                )
            prices[contract_id] += basis_shift
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

        def shifted_levels(
            base_values: tuple[tuple[str, Decimal], ...],
            shifts: tuple[tuple[str, Decimal], ...],
            *,
            label: str,
            target_members: Mapping[str, tuple[str, ...]] | None = None,
            nonnegative: bool = False,
        ) -> tuple[tuple[str, Decimal], ...]:
            values = dict(base_values)
            members_by_target = target_members or {}
            for target_id, shift in shifts:
                member_ids = members_by_target.get(target_id)
                if member_ids is None:
                    member_ids = (target_id,) if target_id in values else None
                if member_ids is None:
                    raise ScenarioError(f"scenario_{label}_target_unknown:{target_id}")
                for member_id in member_ids:
                    values[member_id] += shift
                    if nonnegative and values[member_id] < _ZERO:
                        raise ScenarioError(
                            f"scenario_{label}_projected_negative:{member_id}"
                        )
            return tuple(sorted(values.items()))

        rate_levels = shifted_levels(
            base_projection.rate_levels,
            shock.rate_shifts,
            label="rate",
            target_members=dict(base_projection.rate_target_members),
        )
        dividend_yields = shifted_levels(
            base_projection.dividend_yields,
            shock.dividend_yield_shifts,
            label="dividend_yield",
        )
        borrow_rates = shifted_levels(
            base_projection.borrow_rates,
            shock.borrow_rate_shifts,
            label="borrow_rate",
            nonnegative=True,
        )
        funding_rates = shifted_levels(
            base_projection.funding_rates,
            shock.funding_rate_shifts,
            label="funding_rate",
        )
        spreads = dict(base_projection.spreads)
        for instrument_id, multiplier in shock.spread_multipliers:
            if instrument_id not in spreads:
                raise ScenarioError(f"scenario_spread_target_unknown:{instrument_id}")
            spreads[instrument_id] *= multiplier

        surface_ids = {item.surface_id for item in base_projection.volatility_points}
        for label, shifts in (
            ("volatility", shock.volatility_shifts),
            ("volatility_skew", shock.volatility_skew_shifts),
            ("volatility_term", shock.volatility_term_shifts),
        ):
            for surface_id, _ in shifts:
                if surface_id not in surface_ids:
                    raise ScenarioError(f"scenario_{label}_target_unknown:{surface_id}")
        parallel_by_surface = dict(shock.volatility_shifts)
        skew_by_surface = dict(shock.volatility_skew_shifts)
        term_by_surface = dict(shock.volatility_term_shifts)
        projected_volatility: list[VolatilityPointProjection] = []
        valuation_time = _timestamp(valuation_at, "scenario.valuation_at")
        seconds_per_year = Decimal(365 * 24 * 60 * 60)
        for point in base_projection.volatility_points:
            try:
                underlying_price = prices[point.underlying_instrument_id]
            except KeyError as exc:
                raise ScenarioError(
                    "scenario_volatility_underlying_price_missing:"
                    f"{point.surface_id}:{point.underlying_instrument_id}"
                ) from exc
            expiry_time = _timestamp(
                point.expiry_at,
                "scenario.volatility.expiry_at",
            )
            seconds_to_expiry = max(
                Decimal(str((expiry_time - valuation_time).total_seconds())),
                _ZERO,
            )
            year_fraction = seconds_to_expiry / seconds_per_year
            relative_strike = (point.strike / underlying_price) - _ONE
            projected_level = (
                point.base_volatility
                + parallel_by_surface.get(point.surface_id, _ZERO)
                + skew_by_surface.get(point.surface_id, _ZERO) * relative_strike
                + term_by_surface.get(point.surface_id, _ZERO) * year_fraction
            )
            if projected_level < _ZERO:
                raise ScenarioError(
                    "scenario_projected_volatility_negative:"
                    f"{point.surface_id}:{point.expiry_at}:"
                    f"{_decimal_text(point.strike)}"
                )
            projected_volatility.append(
                VolatilityPointProjection(
                    surface_id=point.surface_id,
                    underlying_instrument_id=point.underlying_instrument_id,
                    expiry_at=point.expiry_at,
                    strike=point.strike,
                    base_volatility=point.base_volatility,
                    projected_volatility=projected_level,
                    source_component_hash=point.source_component_hash,
                )
            )

        futures_basis_levels: dict[str, Decimal] = {}
        for contract_id, underlying_id in futures_underlyings.items():
            futures_basis_levels[contract_id] = (
                prices[contract_id] - prices[underlying_id]
            )
        price_targets = set(prices)
        for instrument_id, _ in shock.liquidity_haircuts:
            if instrument_id not in price_targets:
                raise ScenarioError(
                    f"scenario_liquidity_target_unknown:{instrument_id}"
                )
        return ShockedMarketState(
            parent_state_id=market_state.state_id,
            parent_state_hash=base_state_hash,
            base_projection_hash=base_projection.content_hash,
            valuation_at=valuation_at,
            base_currency=market_state.base_currency,
            scenario_hash=shock.content_hash,
            prices=tuple(sorted(prices.items())),
            price_source_kinds=base_projection.price_source_kinds,
            fx_rates=tuple(sorted(fx_rates.items())),
            rate_levels=rate_levels,
            dividend_yields=dividend_yields,
            borrow_rates=borrow_rates,
            funding_rates=funding_rates,
            spreads=tuple(sorted(spreads.items())),
            futures_basis_levels=tuple(sorted(futures_basis_levels.items())),
            volatility_points=tuple(projected_volatility),
            volatility_shifts=shock.volatility_shifts,
            volatility_skew_shifts=shock.volatility_skew_shifts,
            volatility_term_shifts=shock.volatility_term_shifts,
            rate_shifts=shock.rate_shifts,
            dividend_yield_shifts=shock.dividend_yield_shifts,
            borrow_rate_shifts=shock.borrow_rate_shifts,
            funding_rate_shifts=shock.funding_rate_shifts,
            futures_curve_returns=shock.futures_curve_returns,
            futures_basis_shifts=shock.futures_basis_shifts,
            spread_multipliers=shock.spread_multipliers,
            liquidity_haircuts=shock.liquidity_haircuts,
            liquidity_cost_multiplier=shock.liquidity_cost_multiplier,
            margin_multiplier=shock.margin_multiplier,
            source_component_hashes=base_projection.source_component_hashes,
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

    Price, FX, futures-curve and spread changes compound in path order;
    absolute price/basis shifts apply after that step's return; rate,
    volatility, dividend, borrow, and funding shifts add; and
    margin/liquidity multipliers compound.  Positions and the ledger remain
    fixed; this is a stress revaluation engine, not a trading or
    forced-liquidation simulator.
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

        base_projection = build_common_market_projection(
            market_state,
            fallback_marks={
                position.instrument_id: position.mark_price
                for position in snapshot.positions
            },
        )
        base_marks = self.joint_engine._base_marks(
            snapshot,
            market_state,
            projection=base_projection,
        )
        base_fx = self.joint_engine._base_fx(snapshot, market_state)
        try:
            base_valuation = snapshot.valuation(fx_rates=base_fx, marks=base_marks)
        except PortfolioAccountingError as exc:
            raise ScenarioError(str(exc)) from exc
        if not base_valuation.reconciled:
            raise ScenarioError("path_engine_base_portfolio_not_reconciled")
        if base_valuation.nav <= _ZERO:
            raise ScenarioError("path_engine_base_nav_nonpositive")

        base_price_levels = dict(base_projection.prices)
        price_levels = dict(base_price_levels)
        touched_prices: set[str] = set()
        fx_levels = dict(base_fx)
        touched_fx: set[str] = set()
        volatility_shifts: dict[str, Decimal] = {}
        volatility_skew_shifts: dict[str, Decimal] = {}
        volatility_term_shifts: dict[str, Decimal] = {}
        rate_shifts: dict[str, Decimal] = {}
        dividend_yield_shifts: dict[str, Decimal] = {}
        borrow_rate_shifts: dict[str, Decimal] = {}
        funding_rate_shifts: dict[str, Decimal] = {}
        futures_curve_multipliers: dict[str, Decimal] = {}
        futures_basis_shifts: dict[str, Decimal] = {}
        spread_multipliers: dict[str, Decimal] = {}
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
            self._apply_futures_price_shock(
                price_levels,
                touched_prices,
                base_projection,
                shock,
            )
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
            self._add_factor_shifts(
                volatility_skew_shifts,
                shock.volatility_skew_shifts,
            )
            self._add_factor_shifts(
                volatility_term_shifts,
                shock.volatility_term_shifts,
            )
            self._add_factor_shifts(rate_shifts, shock.rate_shifts)
            self._add_factor_shifts(
                dividend_yield_shifts,
                shock.dividend_yield_shifts,
            )
            self._add_factor_shifts(
                borrow_rate_shifts,
                shock.borrow_rate_shifts,
            )
            self._add_factor_shifts(
                funding_rate_shifts,
                shock.funding_rate_shifts,
            )
            self._add_factor_shifts(
                futures_basis_shifts,
                shock.futures_basis_shifts,
            )
            self._compound_returns(
                futures_curve_multipliers,
                shock.futures_curve_returns,
            )
            self._compound_multipliers(
                spread_multipliers,
                shock.spread_multipliers,
            )
            self._compound_haircuts(liquidity_haircuts, shock.liquidity_haircuts)
            liquidity_cost_multiplier *= shock.liquidity_cost_multiplier
            margin_multiplier *= shock.margin_multiplier
            source_hashes.update(shock.source_hashes)
            source_hashes.add(shock.content_hash)
            factor_prices = dict(base_price_levels)
            for curve_id, multiplier in futures_curve_multipliers.items():
                for contract_id in dict(base_projection.futures_curve_members)[
                    curve_id
                ]:
                    factor_prices[contract_id] *= multiplier
            for contract_id, shift in futures_basis_shifts.items():
                factor_prices[contract_id] += shift
            cumulative_shock = JointMarketShock(
                scenario_id=(
                    f"{scenario.path_id}.step{step.sequence}.{step.step_id}.cumulative"
                ),
                price_absolute_shifts=tuple(
                    sorted(
                        (
                            instrument_id,
                            price_levels[instrument_id] - factor_prices[instrument_id],
                        )
                        for instrument_id in touched_prices
                        if price_levels[instrument_id] != factor_prices[instrument_id]
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
                volatility_skew_shifts=tuple(sorted(volatility_skew_shifts.items())),
                volatility_term_shifts=tuple(sorted(volatility_term_shifts.items())),
                rate_shifts=tuple(sorted(rate_shifts.items())),
                dividend_yield_shifts=tuple(sorted(dividend_yield_shifts.items())),
                borrow_rate_shifts=tuple(sorted(borrow_rate_shifts.items())),
                funding_rate_shifts=tuple(sorted(funding_rate_shifts.items())),
                futures_curve_returns=tuple(
                    sorted(
                        (curve_id, multiplier - _ONE)
                        for curve_id, multiplier in futures_curve_multipliers.items()
                    )
                ),
                futures_basis_shifts=tuple(sorted(futures_basis_shifts.items())),
                spread_multipliers=tuple(sorted(spread_multipliers.items())),
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
    def _apply_futures_price_shock(
        levels: dict[str, Decimal],
        touched: set[str],
        projection: CommonMarketProjection,
        shock: JointMarketShock,
    ) -> None:
        curve_members = dict(projection.futures_curve_members)
        for curve_id, curve_return in shock.futures_curve_returns:
            contracts = curve_members.get(curve_id)
            if contracts is None:
                raise ScenarioError(f"path_futures_curve_target_unknown:{curve_id}")
            for contract_id in contracts:
                levels[contract_id] *= _ONE + curve_return
                touched.add(contract_id)
        futures_contracts = dict(projection.futures_underlyings)
        for contract_id, basis_shift in shock.futures_basis_shifts:
            if contract_id not in futures_contracts:
                raise ScenarioError(f"path_futures_basis_target_unknown:{contract_id}")
            levels[contract_id] += basis_shift
            touched.add(contract_id)
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
    def _compound_returns(
        cumulative: dict[str, Decimal],
        increments: tuple[tuple[str, Decimal], ...],
    ) -> None:
        for factor_id, factor_return in increments:
            cumulative[factor_id] = cumulative.get(factor_id, _ONE) * (
                _ONE + factor_return
            )

    @staticmethod
    def _compound_multipliers(
        cumulative: dict[str, Decimal],
        increments: tuple[tuple[str, Decimal], ...],
    ) -> None:
        for factor_id, multiplier in increments:
            cumulative[factor_id] = cumulative.get(factor_id, _ONE) * multiplier

    @staticmethod
    def _compound_haircuts(
        cumulative: dict[str, Decimal],
        increments: tuple[tuple[str, Decimal], ...],
    ) -> None:
        for instrument_id, haircut in increments:
            prior = cumulative.get(instrument_id, _ZERO)
            cumulative[instrument_id] = _ONE - ((_ONE - prior) * (_ONE - haircut))


@dataclass(frozen=True, slots=True)
class EconomicProjectionPolicy:
    policy_id: str
    version: str
    maximum_absolute_basis_fraction: Decimal
    maximum_volatility_curvature: Decimal
    require_derivative_repricers: bool = True
    require_liquidation_costs_for_liquidity_shock: bool = True
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "economic_policy.policy_id")
        _require_id(self.version, "economic_policy.version")
        _decimal(
            self.maximum_absolute_basis_fraction,
            "economic_policy.maximum_absolute_basis_fraction",
            nonnegative=True,
        )
        _decimal(
            self.maximum_volatility_curvature,
            "economic_policy.maximum_volatility_curvature",
            nonnegative=True,
        )
        for name in (
            "require_derivative_repricers",
            "require_liquidation_costs_for_liquidity_shock",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ScenarioError(f"economic_policy_{name}_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "policy_id": self.policy_id,
                    "version": self.version,
                    "maximum_absolute_basis_fraction": _decimal_text(
                        self.maximum_absolute_basis_fraction
                    ),
                    "maximum_volatility_curvature": _decimal_text(
                        self.maximum_volatility_curvature
                    ),
                    "require_derivative_repricers": (
                        self.require_derivative_repricers
                    ),
                    "require_liquidation_costs_for_liquidity_shock": (
                        self.require_liquidation_costs_for_liquidity_shock
                    ),
                },
                label="economic_projection_policy",
            ),
        )


@dataclass(frozen=True, slots=True)
class EconomicConstraintEvidence:
    scenario_result_hash: str
    policy_hash: str
    futures_basis_fractions: tuple[tuple[str, Decimal], ...]
    derivative_repricer_hash: str
    volatility_constraint_hash: str
    liquidity_constraint_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "scenario_result_hash",
            "policy_hash",
            "derivative_repricer_hash",
            "volatility_constraint_hash",
            "liquidity_constraint_hash",
        ):
            _require_hash(getattr(self, name), f"economic_evidence.{name}")
        if tuple(sorted(self.futures_basis_fractions)) != (
            self.futures_basis_fractions
        ):
            raise ScenarioError("economic_evidence_basis_not_sorted")
        if len({key for key, _value in self.futures_basis_fractions}) != len(
            self.futures_basis_fractions
        ):
            raise ScenarioError("economic_evidence_basis_duplicate")
        for contract_id, fraction in self.futures_basis_fractions:
            _require_id(contract_id, "economic_evidence.contract_id")
            _decimal(fraction, "economic_evidence.basis_fraction")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "scenario_result_hash": self.scenario_result_hash,
                    "policy_hash": self.policy_hash,
                    "futures_basis_fractions": [
                        {
                            "contract_id": key,
                            "basis_fraction": _decimal_text(value),
                        }
                        for key, value in self.futures_basis_fractions
                    ],
                    "derivative_repricer_hash": self.derivative_repricer_hash,
                    "volatility_constraint_hash": (
                        self.volatility_constraint_hash
                    ),
                    "liquidity_constraint_hash": self.liquidity_constraint_hash,
                },
                label="economic_constraint_evidence",
            ),
        )


@dataclass(frozen=True, slots=True)
class ConstrainedScenarioResult:
    result: JointScenarioResult
    economic_evidence: EconomicConstraintEvidence
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.result, JointScenarioResult) or not isinstance(
            self.economic_evidence, EconomicConstraintEvidence
        ):
            raise ScenarioError("constrained_scenario_result_types_invalid")
        if self.economic_evidence.scenario_result_hash != self.result.content_hash:
            raise ScenarioError("constrained_scenario_result_binding_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "scenario_result_hash": self.result.content_hash,
                    "economic_evidence_hash": self.economic_evidence.content_hash,
                },
                label="constrained_scenario_result",
            ),
        )


@dataclass(frozen=True, slots=True)
class ConstrainedJointScenarioEngine:
    """Reprice first, then fail closed on cross-product economic constraints."""

    policy: EconomicProjectionPolicy
    joint_engine: JointScenarioEngine = field(default_factory=JointScenarioEngine)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, EconomicProjectionPolicy):
            raise ScenarioError("constrained_scenario_policy_required")
        if not isinstance(self.joint_engine, JointScenarioEngine):
            raise ScenarioError("constrained_scenario_joint_engine_required")

    def evaluate(
        self,
        snapshot: PortfolioSnapshot,
        *,
        market_state: ImmutableMarketStateLike,
        shock: JointMarketShock,
        repricers: Mapping[str, PositionRepricer] | None = None,
        base_liquidation_costs: Mapping[str, Decimal] | None = None,
        scenario_valuation_at: str | None = None,
    ) -> ConstrainedScenarioResult:
        if not isinstance(self.policy, EconomicProjectionPolicy):
            raise ScenarioError("constrained_scenario_policy_required")
        result = self.joint_engine.evaluate(
            snapshot,
            market_state=market_state,
            shock=shock,
            repricers=repricers,
            base_liquidation_costs=base_liquidation_costs,
            scenario_valuation_at=scenario_valuation_at,
        )
        state = result.shocked_state
        prices = dict(state.prices)
        futures_underlyings = dict(
            build_common_market_projection(
                market_state,
                fallback_marks={
                    item.instrument_id: item.mark_price
                    for item in snapshot.positions
                },
            ).futures_underlyings
        )
        basis_rows: list[tuple[str, Decimal]] = []
        for contract_id, underlying_id in sorted(futures_underlyings.items()):
            if contract_id not in prices or underlying_id not in prices:
                raise ScenarioError(
                    f"economic_constraint_future_underlying_missing:{contract_id}"
                )
            fraction = (prices[contract_id] - prices[underlying_id]) / prices[
                underlying_id
            ]
            if abs(fraction) > self.policy.maximum_absolute_basis_fraction:
                raise ScenarioError(
                    f"economic_constraint_future_basis_exceeded:{contract_id}"
                )
            basis_rows.append((contract_id, fraction))

        repricer_rows = tuple(
            sorted(
                (
                    item.instrument_id,
                    item.asset_class.value,
                    item.repricer,
                )
                for item in result.position_results
            )
        )
        derivative_factor_shock = bool(
            shock.price_returns
            or shock.price_absolute_shifts
            or shock.rate_shifts
            or shock.dividend_yield_shifts
            or shock.volatility_shifts
            or shock.volatility_skew_shifts
            or shock.volatility_term_shifts
            or shock.futures_curve_returns
            or shock.futures_basis_shifts
        )
        if (
            self.policy.require_derivative_repricers
            and derivative_factor_shock
            and any(
                asset_class in {AssetClass.FUTURE.value, AssetClass.OPTION.value}
                and repricer == "direct_mark_shock"
                for _instrument_id, asset_class, repricer in repricer_rows
            )
        ):
            raise ScenarioError("economic_constraint_derivative_repricer_required")
        derivative_hash = sha256_prefixed(
            repricer_rows, label="economic_derivative_repricing"
        )

        _validate_volatility_no_arbitrage(
            state,
            maximum_curvature=self.policy.maximum_volatility_curvature,
        )
        volatility_hash = sha256_prefixed(
            [
                {
                    "surface_id": item.surface_id,
                    "underlying_id": item.underlying_instrument_id,
                    "expiry_at": item.expiry_at,
                    "strike": _decimal_text(item.strike),
                    "volatility": _decimal_text(item.projected_volatility),
                }
                for item in state.volatility_points
            ],
            label="economic_volatility_constraints",
        )
        liquidity_shock = bool(
            shock.liquidity_haircuts
            or shock.spread_multipliers
            or shock.liquidity_cost_multiplier != _ONE
        )
        if (
            liquidity_shock
            and self.policy.require_liquidation_costs_for_liquidity_shock
            and not base_liquidation_costs
        ):
            raise ScenarioError(
                "economic_constraint_liquidation_cost_evidence_required"
            )
        liquidity_hash = sha256_prefixed(
            {
                "liquidity_shock": liquidity_shock,
                "liquidity_reserve": _decimal_text(result.liquidity_reserve),
                "base_liquidation_costs": [
                    {
                        "instrument_id": key,
                        "cost": _decimal_text(value),
                    }
                    for key, value in sorted((base_liquidation_costs or {}).items())
                ],
            },
            label="economic_liquidity_constraints",
        )
        evidence = EconomicConstraintEvidence(
            scenario_result_hash=result.content_hash,
            policy_hash=self.policy.content_hash,
            futures_basis_fractions=tuple(basis_rows),
            derivative_repricer_hash=derivative_hash,
            volatility_constraint_hash=volatility_hash,
            liquidity_constraint_hash=liquidity_hash,
        )
        return ConstrainedScenarioResult(result=result, economic_evidence=evidence)


def _validate_volatility_no_arbitrage(
    state: ShockedMarketState,
    *,
    maximum_curvature: Decimal,
) -> None:
    valuation_at = _timestamp(state.valuation_at, "economic_state.valuation_at")
    by_underlying_strike: dict[
        tuple[str, Decimal], list[VolatilityPointProjection]
    ] = {}
    by_underlying_expiry: dict[
        tuple[str, str], list[VolatilityPointProjection]
    ] = {}
    for point in state.volatility_points:
        by_underlying_strike.setdefault(
            (point.underlying_instrument_id, point.strike), []
        ).append(point)
        by_underlying_expiry.setdefault(
            (point.underlying_instrument_id, point.expiry_at), []
        ).append(point)
    for points in by_underlying_strike.values():
        previous_total_variance: Decimal | None = None
        for point in sorted(points, key=lambda item: item.expiry_at):
            days = Decimal(
                str(
                    (
                        _timestamp(point.expiry_at, "economic_vol.expiry")
                        - valuation_at
                    ).total_seconds()
                    / 86_400
                )
            )
            if days <= _ZERO:
                raise ScenarioError("economic_constraint_volatility_expired_point")
            total_variance = point.projected_volatility**2 * days / Decimal("365")
            if (
                previous_total_variance is not None
                and total_variance < previous_total_variance
            ):
                raise ScenarioError(
                    "economic_constraint_volatility_calendar_arbitrage"
                )
            previous_total_variance = total_variance
    for points in by_underlying_expiry.values():
        ordered = sorted(points, key=lambda item: item.strike)
        for left, middle, right in zip(
            ordered, ordered[1:], ordered[2:], strict=False
        ):
            left_width = middle.strike - left.strike
            right_width = right.strike - middle.strike
            if left_width <= _ZERO or right_width <= _ZERO:
                raise ScenarioError("economic_constraint_volatility_strike_duplicate")
            left_slope = (
                middle.projected_volatility - left.projected_volatility
            ) / left_width
            right_slope = (
                right.projected_volatility - middle.projected_volatility
            ) / right_width
            if abs(right_slope - left_slope) > maximum_curvature:
                raise ScenarioError(
                    "economic_constraint_volatility_butterfly_curvature"
                )


class ScenarioPathMode(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    HISTORICAL = "HISTORICAL"
    BOOTSTRAP = "BOOTSTRAP"
    STOCHASTIC = "STOCHASTIC"


@dataclass(frozen=True, slots=True)
class ScenarioFactorObservation:
    observation_id: str
    observed_at: str
    regime_id: str
    price_returns: tuple[tuple[str, Decimal], ...] = ()
    fx_returns: tuple[tuple[str, Decimal], ...] = ()
    volatility_shifts: tuple[tuple[str, Decimal], ...] = ()
    rate_shifts: tuple[tuple[str, Decimal], ...] = ()
    funding_rate_shifts: tuple[tuple[str, Decimal], ...] = ()
    spread_multipliers: tuple[tuple[str, Decimal], ...] = ()
    liquidity_haircuts: tuple[tuple[str, Decimal], ...] = ()
    margin_multiplier: Decimal = _ONE
    source_hash: str = ""
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.observation_id, "scenario_observation.observation_id")
        object.__setattr__(
            self,
            "observed_at",
            _timestamp_text(
                self.observed_at, "scenario_observation.observed_at"
            ),
        )
        _require_id(self.regime_id, "scenario_observation.regime_id")
        for name in (
            "price_returns",
            "fx_returns",
            "volatility_shifts",
            "rate_shifts",
            "funding_rate_shifts",
        ):
            object.__setattr__(
                self,
                name,
                _normalize_pairs(
                    getattr(self, name), f"scenario_observation.{name}"
                ),
            )
        if any(
            value <= -_ONE
            for name in ("price_returns", "fx_returns")
            for _key, value in getattr(self, name)
        ):
            raise ScenarioError("scenario_observation_return_at_or_below_minus_one")
        object.__setattr__(
            self,
            "spread_multipliers",
            _normalize_pairs(
                self.spread_multipliers,
                "scenario_observation.spread_multipliers",
                positive=True,
            ),
        )
        haircuts = _normalize_pairs(
            self.liquidity_haircuts,
            "scenario_observation.liquidity_haircuts",
            nonnegative=True,
        )
        if any(value > _ONE for _, value in haircuts):
            raise ScenarioError("scenario_observation_haircut_above_one")
        object.__setattr__(self, "liquidity_haircuts", haircuts)
        _decimal(
            self.margin_multiplier,
            "scenario_observation.margin_multiplier",
            positive=True,
        )
        _require_hash(self.source_hash, "scenario_observation.source_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "observation_id": self.observation_id,
                    "observed_at": self.observed_at,
                    "regime_id": self.regime_id,
                    **{
                        name: [
                            {"key": key, "value": _decimal_text(value)}
                            for key, value in getattr(self, name)
                        ]
                        for name in (
                            "price_returns",
                            "fx_returns",
                            "volatility_shifts",
                            "rate_shifts",
                            "funding_rate_shifts",
                            "spread_multipliers",
                            "liquidity_haircuts",
                        )
                    },
                    "margin_multiplier": _decimal_text(self.margin_multiplier),
                    "source_hash": self.source_hash,
                },
                label="scenario_factor_observation",
            ),
        )


@dataclass(frozen=True, slots=True)
class ScenarioPathGenerationSpec:
    path_id: str
    mode: ScenarioPathMode
    step_count: int
    seed: int
    window_start: str
    window_end: str
    regime_id: str
    model_hash: str
    block_length: int = 1
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.path_id, "scenario_generation.path_id")
        if not isinstance(self.mode, ScenarioPathMode):
            raise ScenarioError("scenario_generation_mode_invalid")
        for name in ("step_count", "seed", "block_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ScenarioError(f"scenario_generation_{name}_invalid")
        if self.step_count <= 0 or self.step_count > _HARD_MAX_PATH_STEPS:
            raise ScenarioError("scenario_generation_step_count_invalid")
        if self.block_length <= 0 or self.block_length > self.step_count:
            raise ScenarioError("scenario_generation_block_length_invalid")
        start = _timestamp(self.window_start, "scenario_generation.window_start")
        end = _timestamp(self.window_end, "scenario_generation.window_end")
        if start > end:
            raise ScenarioError("scenario_generation_window_inverted")
        object.__setattr__(
            self,
            "window_start",
            start.isoformat(),
        )
        object.__setattr__(
            self,
            "window_end",
            end.isoformat(),
        )
        _require_id(self.regime_id, "scenario_generation.regime_id")
        _require_hash(self.model_hash, "scenario_generation.model_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "path_id": self.path_id,
                    "mode": self.mode.value,
                    "step_count": self.step_count,
                    "seed": self.seed,
                    "window_start": self.window_start,
                    "window_end": self.window_end,
                    "regime_id": self.regime_id,
                    "model_hash": self.model_hash,
                    "block_length": self.block_length,
                },
                label="scenario_path_generation_spec",
            ),
        )


@dataclass(frozen=True, slots=True)
class GeneratedScenarioEvent:
    sequence: int
    event_id: str
    predecessor_hash: str
    shock: JointMarketShock
    observation_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.sequence <= 0 or isinstance(self.sequence, bool):
            raise ScenarioError("generated_scenario_sequence_invalid")
        _require_id(self.event_id, "generated_scenario.event_id")
        _require_hash(
            self.predecessor_hash, "generated_scenario.predecessor_hash"
        )
        if not isinstance(self.shock, JointMarketShock):
            raise ScenarioError("generated_scenario_shock_invalid")
        if tuple(sorted(set(self.observation_hashes))) != self.observation_hashes:
            raise ScenarioError("generated_scenario_observation_hashes_invalid")
        if not self.observation_hashes:
            raise ScenarioError("generated_scenario_observation_hashes_required")
        for item in self.observation_hashes:
            _require_hash(item, "generated_scenario.observation_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "sequence": self.sequence,
                    "event_id": self.event_id,
                    "predecessor_hash": self.predecessor_hash,
                    "shock_hash": self.shock.content_hash,
                    "observation_hashes": list(self.observation_hashes),
                },
                label="generated_scenario_event",
            ),
        )


@dataclass(frozen=True, slots=True)
class GeneratedScenarioPath:
    specification: ScenarioPathGenerationSpec
    chain_root_hash: str
    events: tuple[GeneratedScenarioEvent, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.specification, ScenarioPathGenerationSpec):
            raise ScenarioError("generated_scenario_specification_invalid")
        if any(
            not isinstance(item, GeneratedScenarioEvent) for item in self.events
        ):
            raise ScenarioError("generated_scenario_event_invalid")
        expected_root = sha256_prefixed(
            {
                "specification_hash": self.specification.content_hash,
                "model_hash": self.specification.model_hash,
                "seed": self.specification.seed,
                "window": [
                    self.specification.window_start,
                    self.specification.window_end,
                ],
                "regime_id": self.specification.regime_id,
            },
            label="generated_scenario_chain_root",
        )
        if self.chain_root_hash != expected_root:
            raise ScenarioError("generated_scenario_chain_root_mismatch")
        if len(self.events) != self.specification.step_count:
            raise ScenarioError("generated_scenario_event_count_mismatch")
        predecessor = self.chain_root_hash
        for expected, event in enumerate(self.events, start=1):
            if event.sequence != expected or event.predecessor_hash != predecessor:
                raise ScenarioError("generated_scenario_event_chain_broken")
            predecessor = event.content_hash
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "specification_hash": self.specification.content_hash,
                    "chain_root_hash": self.chain_root_hash,
                    "event_hashes": [item.content_hash for item in self.events],
                },
                label="generated_scenario_path",
            ),
        )

    def to_stress_scenario(
        self,
        *,
        expected_base_state_hash: str,
        expected_ledger_hash: str,
        effective_times: tuple[str, ...],
        risk_limits: PathRiskLimits,
    ) -> PathStressScenario:
        if len(effective_times) != len(self.events):
            raise ScenarioError("generated_scenario_effective_time_count_mismatch")
        steps: list[PathShockStep] = []
        predecessor = expected_base_state_hash
        for event, effective_at in zip(
            self.events, effective_times, strict=True
        ):
            step = PathShockStep(
                sequence=event.sequence,
                step_id=event.event_id,
                effective_at=effective_at,
                predecessor_hash=predecessor,
                shock=event.shock,
            )
            steps.append(step)
            predecessor = step.content_hash
        return PathStressScenario(
            path_id=self.specification.path_id,
            expected_base_state_hash=expected_base_state_hash,
            expected_ledger_hash=expected_ledger_hash,
            steps=tuple(steps),
            risk_limits=risk_limits,
        )


class ScenarioPathFactory:
    """Create deterministic, historical, bootstrap or stochastic shock paths."""

    def generate(
        self,
        specification: ScenarioPathGenerationSpec,
        *,
        observations: tuple[ScenarioFactorObservation, ...],
    ) -> GeneratedScenarioPath:
        if not isinstance(specification, ScenarioPathGenerationSpec):
            raise ScenarioError("scenario_generation_specification_invalid")
        if not observations:
            raise ScenarioError("scenario_generation_observations_required")
        if any(
            not isinstance(item, ScenarioFactorObservation)
            for item in observations
        ):
            raise ScenarioError("scenario_generation_observation_invalid")
        ordered = tuple(sorted(observations, key=lambda item: item.observed_at))
        if len({item.observation_id for item in ordered}) != len(ordered):
            raise ScenarioError("scenario_generation_observation_id_duplicate")
        start = _timestamp(
            specification.window_start, "scenario_generation.window_start"
        )
        end = _timestamp(
            specification.window_end, "scenario_generation.window_end"
        )
        eligible = tuple(
            item
            for item in ordered
            if start
            <= _timestamp(item.observed_at, "scenario_observation.observed_at")
            <= end
            and item.regime_id == specification.regime_id
        )
        if not eligible:
            raise ScenarioError("scenario_generation_window_regime_empty")
        selected = _select_scenario_observations(
            specification, eligible=eligible
        )
        chain_root = sha256_prefixed(
            {
                "specification_hash": specification.content_hash,
                "model_hash": specification.model_hash,
                "seed": specification.seed,
                "window": [
                    specification.window_start,
                    specification.window_end,
                ],
                "regime_id": specification.regime_id,
            },
            label="generated_scenario_chain_root",
        )
        events: list[GeneratedScenarioEvent] = []
        predecessor = chain_root
        for sequence, (shock, source_hashes) in enumerate(selected, start=1):
            event = GeneratedScenarioEvent(
                sequence=sequence,
                event_id=f"{specification.path_id}.step.{sequence}",
                predecessor_hash=predecessor,
                shock=shock,
                observation_hashes=tuple(sorted(set(source_hashes))),
            )
            events.append(event)
            predecessor = event.content_hash
        return GeneratedScenarioPath(
            specification=specification,
            chain_root_hash=chain_root,
            events=tuple(events),
        )


def _select_scenario_observations(
    specification: ScenarioPathGenerationSpec,
    *,
    eligible: tuple[ScenarioFactorObservation, ...],
) -> tuple[tuple[JointMarketShock, tuple[str, ...]], ...]:
    rng = random.Random(specification.seed)
    chosen: list[ScenarioFactorObservation] = []
    if specification.mode in {
        ScenarioPathMode.DETERMINISTIC,
        ScenarioPathMode.HISTORICAL,
    }:
        if len(eligible) < specification.step_count:
            raise ScenarioError("scenario_generation_history_too_short")
        chosen = list(eligible[-specification.step_count :])
    elif specification.mode is ScenarioPathMode.BOOTSTRAP:
        while len(chosen) < specification.step_count:
            start = rng.randrange(len(eligible))
            for offset in range(specification.block_length):
                chosen.append(eligible[(start + offset) % len(eligible)])
                if len(chosen) == specification.step_count:
                    break
    else:
        return _stochastic_scenario_shocks(
            specification, eligible=eligible, rng=rng
        )
    return tuple(
        (
            _observation_to_shock(
                item,
                scenario_id=f"{specification.path_id}.shock.{index}",
                model_hash=specification.model_hash,
            ),
            (item.content_hash,),
        )
        for index, item in enumerate(chosen, start=1)
    )


def _observation_to_shock(
    observation: ScenarioFactorObservation,
    *,
    scenario_id: str,
    model_hash: str,
) -> JointMarketShock:
    return JointMarketShock(
        scenario_id=scenario_id,
        price_returns=observation.price_returns,
        fx_returns=observation.fx_returns,
        volatility_shifts=observation.volatility_shifts,
        rate_shifts=observation.rate_shifts,
        funding_rate_shifts=observation.funding_rate_shifts,
        spread_multipliers=observation.spread_multipliers,
        liquidity_haircuts=observation.liquidity_haircuts,
        margin_multiplier=observation.margin_multiplier,
        source_hashes=tuple(
            sorted((observation.content_hash, observation.source_hash, model_hash))
        ),
    )


def _stochastic_scenario_shocks(
    specification: ScenarioPathGenerationSpec,
    *,
    eligible: tuple[ScenarioFactorObservation, ...],
    rng: random.Random,
) -> tuple[tuple[JointMarketShock, tuple[str, ...]], ...]:
    source_hashes = tuple(sorted(item.content_hash for item in eligible))

    def draw_pairs(
        field_name: str,
        *,
        floor: Decimal | None = None,
        ceiling: Decimal | None = None,
    ) -> tuple[
        tuple[str, Decimal], ...
    ]:
        values_by_key: dict[str, list[Decimal]] = {}
        for observation in eligible:
            for key, value in getattr(observation, field_name):
                values_by_key.setdefault(key, []).append(value)
        result: list[tuple[str, Decimal]] = []
        common_z = rng.gauss(0.0, 1.0)
        for key, values in sorted(values_by_key.items()):
            mean = sum(values, start=_ZERO) / Decimal(len(values))
            variance = (
                sum(((item - mean) ** 2 for item in values), start=_ZERO)
                / Decimal(max(len(values) - 1, 1))
            )
            std = variance.sqrt()
            z = Decimal(str((common_z + rng.gauss(0.0, 1.0)) / 2))
            drawn = mean + std * z
            if floor is not None:
                drawn = max(drawn, floor)
            if ceiling is not None:
                drawn = min(drawn, ceiling)
            result.append((key, drawn))
        return tuple(result)

    results: list[tuple[JointMarketShock, tuple[str, ...]]] = []
    for index in range(1, specification.step_count + 1):
        price_returns = draw_pairs("price_returns", floor=Decimal("-0.999999"))
        fx_returns = draw_pairs("fx_returns", floor=Decimal("-0.999999"))
        volatility = draw_pairs("volatility_shifts")
        rates = draw_pairs("rate_shifts")
        funding = draw_pairs("funding_rate_shifts")
        # Positive multiplicative dimensions are sampled in level space and
        # clamped strictly above zero; the model hash makes this explicit.
        spreads = draw_pairs("spread_multipliers", floor=Decimal("0.000001"))
        liquidity = draw_pairs(
            "liquidity_haircuts",
            floor=_ZERO,
            ceiling=_ONE,
        )
        margins = [item.margin_multiplier for item in eligible]
        mean_margin = sum(margins, start=_ZERO) / Decimal(len(margins))
        margin_variance = (
            sum(
                ((item - mean_margin) ** 2 for item in margins),
                start=_ZERO,
            )
            / Decimal(max(len(margins) - 1, 1))
        )
        margin_multiplier = max(
            Decimal("0.000001"),
            mean_margin
            + margin_variance.sqrt()
            * Decimal(str(rng.gauss(0.0, 1.0))),
        )
        shock = JointMarketShock(
            scenario_id=f"{specification.path_id}.shock.{index}",
            price_returns=price_returns,
            fx_returns=fx_returns,
            volatility_shifts=volatility,
            rate_shifts=rates,
            funding_rate_shifts=funding,
            spread_multipliers=spreads,
            liquidity_haircuts=liquidity,
            margin_multiplier=margin_multiplier,
            source_hashes=tuple(
                sorted((*source_hashes, specification.model_hash))
            ),
        )
        results.append((shock, source_hashes))
    return tuple(results)
