"""Immutable cross-product exposure aggregation for offline research.

The values accepted here are signed, position-level research exposures.  A
producer must already have applied quantity, direction, and contract
multiplier to the Greeks and monetary values.  The original multiplier is
retained so that this transformation remains auditable.

Monetary exposure is never combined across currencies.  The snapshot exposes
currency buckets and requires a caller to select a currency whenever more than
one is present; it deliberately has no implicit FX conversion path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from market_research.research.hashing import sha256_prefixed

from .common import (
    DERIVATIVE_RESEARCH_SCHEMA_VERSION,
    DerivativeResearchError,
    InstrumentKind,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_hash,
    require_stable_id,
)


_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_ZERO = Decimal("0")
_MICROSECONDS_PER_SECOND = Decimal("1000000")
_SECONDS_PER_DAY = 86400
_SUPPORTED_KINDS = frozenset(
    {InstrumentKind.SPOT, InstrumentKind.FUTURE, InstrumentKind.OPTION}
)


PORTFOLIO_EXPOSURE_AGGREGATION_POLICY = {
    "policy_id": "research.portfolio_exposure.v1",
    "monetary_aggregation": "currency_bucket_only_no_implicit_fx",
    "greek_input": "signed_position_level_after_quantity_and_multiplier",
    "expiry_concentration_basis": (
        "abs_signed_premium_plus_margin_required_plus_collateral_cash"
    ),
    "expiry_concentration_scope": "expiring_positions_per_currency",
    "stress_aggregation": "complete_scenario_set_per_position_and_currency",
}
PORTFOLIO_EXPOSURE_AGGREGATION_POLICY_HASH = sha256_prefixed(
    PORTFOLIO_EXPOSURE_AGGREGATION_POLICY,
    label="portfolio_exposure_aggregation_policy",
)


class ExposureGroup(StrEnum):
    """Dimensions along which an exposure snapshot is aggregated."""

    CURRENCY = "CURRENCY"
    UNDERLYING = "UNDERLYING"
    EXPIRY = "EXPIRY"


def _require_currency(value: str, field_name: str) -> str:
    if not _CURRENCY.fullmatch(value):
        raise DerivativeResearchError(f"{field_name}_invalid_currency")
    return value


def _non_negative_decimal(value: object, field_name: str) -> Decimal:
    parsed = exact_decimal(value, field_name)
    if parsed < _ZERO:
        raise DerivativeResearchError(f"{field_name}_must_be_non_negative")
    return parsed


def _canonical_timestamp(value: str, field_name: str) -> str:
    parsed = parse_timestamp(value, field_name)
    return parsed.isoformat().replace("+00:00", "Z")


def _exact_elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    elapsed = end - start
    whole_seconds = elapsed.days * _SECONDS_PER_DAY + elapsed.seconds
    return Decimal(whole_seconds) + (
        Decimal(elapsed.microseconds) / _MICROSECONDS_PER_SECOND
    )


def _decimal_sum(values: Sequence[Decimal]) -> Decimal:
    total = _ZERO
    for value in values:
        total += value
    return total


@dataclass(frozen=True, slots=True)
class StressPnL:
    """One signed stress P&L observation with its immutable scenario binding."""

    scenario_id: str
    pnl: Decimal
    scenario_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.scenario_id, "portfolio_stress.scenario_id")
        pnl = exact_decimal(self.pnl, "portfolio_stress.pnl")
        require_hash(self.scenario_hash, "portfolio_stress.scenario_hash")
        object.__setattr__(self, "pnl", pnl)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="portfolio_stress_pnl"),
        )

    def identity_payload(self) -> dict[str, str]:
        return {
            "scenario_id": self.scenario_id,
            "pnl": decimal_text(self.pnl),
            "scenario_hash": self.scenario_hash,
        }

    def as_dict(self) -> dict[str, str]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def stress_set_hash(stress_pnl: Sequence[StressPnL]) -> str:
    """Return the canonical binding for a complete position stress set."""

    if not stress_pnl:
        raise DerivativeResearchError("portfolio_stress_set_empty")
    if any(not isinstance(item, StressPnL) for item in stress_pnl):
        raise DerivativeResearchError("portfolio_stress_item_invalid")
    scenario_ids = [item.scenario_id for item in stress_pnl]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise DerivativeResearchError("portfolio_stress_scenario_duplicate")
    payload = [
        item.identity_payload()
        for item in sorted(stress_pnl, key=lambda item: item.scenario_id)
    ]
    return sha256_prefixed(payload, label="portfolio_position_stress_set")


@dataclass(frozen=True, slots=True)
class PositionEvidenceHashes:
    """Named evidence bindings required for every position exposure."""

    dataset_hash: str
    instrument_hash: str
    valuation_hash: str
    stress_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        hashes = (
            self.dataset_hash,
            self.instrument_hash,
            self.valuation_hash,
            self.stress_hash,
        )
        for name, value in zip(
            ("dataset", "instrument", "valuation", "stress"),
            hashes,
            strict=True,
        ):
            require_hash(value, f"portfolio_evidence.{name}_hash")
        if len(set(hashes)) != len(hashes):
            raise DerivativeResearchError("portfolio_evidence_hash_duplicate")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="portfolio_evidence"),
        )

    def identity_payload(self) -> dict[str, str]:
        return {
            "dataset_hash": self.dataset_hash,
            "instrument_hash": self.instrument_hash,
            "valuation_hash": self.valuation_hash,
            "stress_hash": self.stress_hash,
        }

    def as_dict(self) -> dict[str, str]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class PositionExposure:
    """A signed, point-in-time exposure for one aggregated research position."""

    position_id: str
    instrument_id: str
    instrument_kind: InstrumentKind
    underlying_id: str
    currency: str
    as_of: str
    capital_use_started_at: str
    expiry_at: str | None
    multiplier: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    rho: Decimal
    premium: Decimal
    margin_required: Decimal
    collateral_cash: Decimal
    capital_use_seconds: Decimal
    stress_pnl: tuple[StressPnL, ...]
    evidence_hashes: PositionEvidenceHashes
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise DerivativeResearchError("portfolio_position_schema_unsupported")
        require_stable_id(self.position_id, "portfolio_position.position_id")
        require_stable_id(self.instrument_id, "portfolio_position.instrument_id")
        require_stable_id(self.underlying_id, "portfolio_position.underlying_id")
        if not isinstance(self.instrument_kind, InstrumentKind):
            raise DerivativeResearchError("portfolio_position_instrument_kind_invalid")
        if self.instrument_kind not in _SUPPORTED_KINDS:
            raise DerivativeResearchError(
                "portfolio_position_instrument_kind_unsupported"
            )
        _require_currency(self.currency, "portfolio_position.currency")

        as_of = parse_timestamp(self.as_of, "portfolio_position.as_of")
        capital_started = parse_timestamp(
            self.capital_use_started_at,
            "portfolio_position.capital_use_started_at",
        )
        if capital_started > as_of:
            raise DerivativeResearchError(
                "portfolio_position_capital_start_after_as_of"
            )

        multiplier = exact_decimal(
            self.multiplier,
            "portfolio_position.multiplier",
            positive=True,
        )
        signed_values = {
            "delta": self.delta,
            "gamma": self.gamma,
            "vega": self.vega,
            "theta": self.theta,
            "rho": self.rho,
            "premium": self.premium,
        }
        parsed_signed = {
            name: exact_decimal(value, f"portfolio_position.{name}")
            for name, value in signed_values.items()
        }
        margin = _non_negative_decimal(
            self.margin_required,
            "portfolio_position.margin_required",
        )
        collateral = _non_negative_decimal(
            self.collateral_cash,
            "portfolio_position.collateral_cash",
        )
        capital_seconds = _non_negative_decimal(
            self.capital_use_seconds,
            "portfolio_position.capital_use_seconds",
        )
        if capital_seconds != _exact_elapsed_seconds(capital_started, as_of):
            raise DerivativeResearchError("portfolio_position_capital_seconds_mismatch")

        if self.instrument_kind is InstrumentKind.SPOT:
            if self.expiry_at is not None:
                raise DerivativeResearchError("portfolio_spot_expiry_forbidden")
            if multiplier != Decimal("1"):
                raise DerivativeResearchError("portfolio_spot_multiplier_must_be_one")
            if parsed_signed["premium"] != _ZERO:
                raise DerivativeResearchError("portfolio_spot_premium_must_be_zero")
            if margin != _ZERO:
                raise DerivativeResearchError("portfolio_spot_margin_must_be_zero")
        else:
            if self.expiry_at is None:
                raise DerivativeResearchError("portfolio_derivative_expiry_required")
            expiry = parse_timestamp(
                self.expiry_at,
                "portfolio_position.expiry_at",
            )
            if expiry <= as_of:
                raise DerivativeResearchError(
                    "portfolio_derivative_expiry_not_after_as_of"
                )
            if (
                self.instrument_kind is InstrumentKind.FUTURE
                and parsed_signed["premium"] != _ZERO
            ):
                raise DerivativeResearchError("portfolio_future_premium_must_be_zero")

        if not isinstance(self.stress_pnl, tuple):
            raise DerivativeResearchError("portfolio_stress_tuple_required")
        if not isinstance(self.evidence_hashes, PositionEvidenceHashes):
            raise DerivativeResearchError("portfolio_evidence_hashes_invalid")
        ordered_stress = tuple(
            sorted(self.stress_pnl, key=lambda item: item.scenario_id)
        )
        computed_stress_hash = stress_set_hash(ordered_stress)
        if computed_stress_hash != self.evidence_hashes.stress_hash:
            raise DerivativeResearchError("portfolio_stress_evidence_hash_mismatch")

        object.__setattr__(self, "multiplier", multiplier)
        for name, value in parsed_signed.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "margin_required", margin)
        object.__setattr__(self, "collateral_cash", collateral)
        object.__setattr__(self, "capital_use_seconds", capital_seconds)
        object.__setattr__(self, "stress_pnl", ordered_stress)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="portfolio_position_exposure",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "position_id": self.position_id,
            "instrument_id": self.instrument_id,
            "instrument_kind": self.instrument_kind.value,
            "underlying_id": self.underlying_id,
            "currency": self.currency,
            "as_of": self.as_of,
            "capital_use_started_at": self.capital_use_started_at,
            "expiry_at": self.expiry_at,
            "multiplier": decimal_text(self.multiplier),
            "delta": decimal_text(self.delta),
            "gamma": decimal_text(self.gamma),
            "vega": decimal_text(self.vega),
            "theta": decimal_text(self.theta),
            "rho": decimal_text(self.rho),
            "premium": decimal_text(self.premium),
            "margin_required": decimal_text(self.margin_required),
            "collateral_cash": decimal_text(self.collateral_cash),
            "capital_use_seconds": decimal_text(self.capital_use_seconds),
            "stress_pnl": [item.as_dict() for item in self.stress_pnl],
            "evidence_hashes": self.evidence_hashes.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ExposureAggregate:
    """Immutable totals for one dimension and one currency."""

    group: ExposureGroup
    group_value: str
    currency: str
    position_count: int
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    rho: Decimal
    premium: Decimal
    margin_required: Decimal
    collateral_cash: Decimal
    capital_use_seconds: Decimal
    member_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.group, ExposureGroup):
            raise DerivativeResearchError("portfolio_aggregate_group_invalid")
        if self.group is ExposureGroup.EXPIRY:
            _canonical_timestamp(
                self.group_value,
                "portfolio_aggregate.expiry_group_value",
            )
        else:
            require_stable_id(
                self.group_value,
                "portfolio_aggregate.group_value",
            )
        _require_currency(self.currency, "portfolio_aggregate.currency")
        if self.position_count <= 0:
            raise DerivativeResearchError("portfolio_aggregate_position_count_invalid")
        if not isinstance(self.member_hashes, tuple):
            raise DerivativeResearchError("portfolio_aggregate_members_tuple_required")
        if len(self.member_hashes) != self.position_count:
            raise DerivativeResearchError("portfolio_aggregate_member_count_mismatch")
        if len(set(self.member_hashes)) != len(self.member_hashes):
            raise DerivativeResearchError("portfolio_aggregate_member_duplicate")
        for value in self.member_hashes:
            require_hash(value, "portfolio_aggregate.member_hash")

        for name in ("delta", "gamma", "vega", "theta", "rho", "premium"):
            object.__setattr__(
                self,
                name,
                exact_decimal(getattr(self, name), f"portfolio_aggregate.{name}"),
            )
        for name in ("margin_required", "collateral_cash", "capital_use_seconds"):
            object.__setattr__(
                self,
                name,
                _non_negative_decimal(
                    getattr(self, name),
                    f"portfolio_aggregate.{name}",
                ),
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="portfolio_aggregate"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "group": self.group.value,
            "group_value": self.group_value,
            "currency": self.currency,
            "position_count": self.position_count,
            "delta": decimal_text(self.delta),
            "gamma": decimal_text(self.gamma),
            "vega": decimal_text(self.vega),
            "theta": decimal_text(self.theta),
            "rho": decimal_text(self.rho),
            "premium": decimal_text(self.premium),
            "margin_required": decimal_text(self.margin_required),
            "collateral_cash": decimal_text(self.collateral_cash),
            "capital_use_seconds": decimal_text(self.capital_use_seconds),
            "member_hashes": list(self.member_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class StressPnLAggregate:
    """Complete scenario P&L total for one currency."""

    scenario_id: str
    scenario_hash: str
    currency: str
    pnl: Decimal
    member_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.scenario_id, "portfolio_stress_total.scenario_id")
        require_hash(self.scenario_hash, "portfolio_stress_total.scenario_hash")
        _require_currency(self.currency, "portfolio_stress_total.currency")
        pnl = exact_decimal(self.pnl, "portfolio_stress_total.pnl")
        if not isinstance(self.member_hashes, tuple) or not self.member_hashes:
            raise DerivativeResearchError(
                "portfolio_stress_total_members_tuple_required"
            )
        if len(set(self.member_hashes)) != len(self.member_hashes):
            raise DerivativeResearchError("portfolio_stress_total_member_duplicate")
        for value in self.member_hashes:
            require_hash(value, "portfolio_stress_total.member_hash")
        object.__setattr__(self, "pnl", pnl)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="portfolio_stress_pnl_aggregate",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "currency": self.currency,
            "pnl": decimal_text(self.pnl),
            "member_hashes": list(self.member_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ExpiryCapitalShare:
    """One expiry's share of a currency-specific capital basis."""

    expiry_at: str
    capital_basis: Decimal
    share: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        canonical_expiry = _canonical_timestamp(
            self.expiry_at,
            "portfolio_expiry_share.expiry_at",
        )
        basis = _non_negative_decimal(
            self.capital_basis,
            "portfolio_expiry_share.capital_basis",
        )
        share = _non_negative_decimal(self.share, "portfolio_expiry_share.share")
        if share > Decimal("1"):
            raise DerivativeResearchError("portfolio_expiry_share_above_one")
        object.__setattr__(self, "expiry_at", canonical_expiry)
        object.__setattr__(self, "capital_basis", basis)
        object.__setattr__(self, "share", share)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="portfolio_expiry_capital_share",
            ),
        )

    def identity_payload(self) -> dict[str, str]:
        return {
            "expiry_at": self.expiry_at,
            "capital_basis": decimal_text(self.capital_basis),
            "share": decimal_text(self.share),
        }

    def as_dict(self) -> dict[str, str]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ExpiryConcentration:
    """Currency-specific expiry concentration with an explicit capital basis."""

    currency: str
    total_capital_basis: Decimal
    shares: tuple[ExpiryCapitalShare, ...]
    maximum_share: Decimal | None
    herfindahl_index: Decimal | None
    unavailable_reason: str | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_currency(self.currency, "portfolio_expiry_concentration.currency")
        total = _non_negative_decimal(
            self.total_capital_basis,
            "portfolio_expiry_concentration.total_capital_basis",
        )
        if not isinstance(self.shares, tuple):
            raise DerivativeResearchError(
                "portfolio_expiry_concentration_shares_tuple_required"
            )
        expiries = [item.expiry_at for item in self.shares]
        if len(set(expiries)) != len(expiries):
            raise DerivativeResearchError(
                "portfolio_expiry_concentration_expiry_duplicate"
            )
        for item in self.shares:
            if not isinstance(item, ExpiryCapitalShare):
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_share_invalid"
                )
        if total == _ZERO:
            if self.shares or self.maximum_share is not None:
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_zero_basis_mismatch"
                )
            if self.herfindahl_index is not None or self.unavailable_reason is None:
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_unavailable_mismatch"
                )
            require_stable_id(
                self.unavailable_reason,
                "portfolio_expiry_concentration.unavailable_reason",
            )
            maximum = None
            herfindahl = None
        else:
            if not self.shares or self.unavailable_reason is not None:
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_available_mismatch"
                )
            if self.maximum_share is None or self.herfindahl_index is None:
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_metrics_required"
                )
            maximum = _non_negative_decimal(
                self.maximum_share,
                "portfolio_expiry_concentration.maximum_share",
            )
            herfindahl = _non_negative_decimal(
                self.herfindahl_index,
                "portfolio_expiry_concentration.herfindahl_index",
            )
            if maximum > Decimal("1") or herfindahl > Decimal("1"):
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_metric_above_one"
                )
            if _decimal_sum([item.capital_basis for item in self.shares]) != total:
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_basis_sum_mismatch"
                )
            if _decimal_sum([item.share for item in self.shares]) != Decimal("1"):
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_share_sum_mismatch"
                )
            if maximum != max(item.share for item in self.shares):
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_maximum_mismatch"
                )
            computed_hhi = _decimal_sum(
                [item.share * item.share for item in self.shares]
            )
            if herfindahl != computed_hhi:
                raise DerivativeResearchError(
                    "portfolio_expiry_concentration_hhi_mismatch"
                )
        object.__setattr__(self, "total_capital_basis", total)
        object.__setattr__(self, "maximum_share", maximum)
        object.__setattr__(self, "herfindahl_index", herfindahl)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="portfolio_expiry_concentration",
            ),
        )

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def identity_payload(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "basis_policy": (
                "abs_signed_premium_plus_margin_required_plus_collateral_cash"
            ),
            "total_capital_basis": decimal_text(self.total_capital_basis),
            "shares": [item.as_dict() for item in self.shares],
            "maximum_share": (
                None if self.maximum_share is None else decimal_text(self.maximum_share)
            ),
            "herfindahl_index": (
                None
                if self.herfindahl_index is None
                else decimal_text(self.herfindahl_index)
            ),
            "unavailable_reason": self.unavailable_reason,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _aggregate_positions(
    *,
    group: ExposureGroup,
    group_value: str,
    currency: str,
    positions: Sequence[PositionExposure],
) -> ExposureAggregate:
    return ExposureAggregate(
        group=group,
        group_value=group_value,
        currency=currency,
        position_count=len(positions),
        delta=_decimal_sum([item.delta for item in positions]),
        gamma=_decimal_sum([item.gamma for item in positions]),
        vega=_decimal_sum([item.vega for item in positions]),
        theta=_decimal_sum([item.theta for item in positions]),
        rho=_decimal_sum([item.rho for item in positions]),
        premium=_decimal_sum([item.premium for item in positions]),
        margin_required=_decimal_sum([item.margin_required for item in positions]),
        collateral_cash=_decimal_sum([item.collateral_cash for item in positions]),
        capital_use_seconds=_decimal_sum(
            [item.capital_use_seconds for item in positions]
        ),
        member_hashes=tuple(sorted(item.content_hash for item in positions)),
    )


@dataclass(frozen=True, slots=True)
class PortfolioExposureSnapshot:
    """Hash-bound cross-product exposure snapshot for offline research only."""

    snapshot_id: str
    as_of: str
    positions: tuple[PositionExposure, ...]
    aggregation_policy_hash: str = PORTFOLIO_EXPOSURE_AGGREGATION_POLICY_HASH
    currency_exposure: tuple[ExposureAggregate, ...] = field(init=False)
    underlying_exposure: tuple[ExposureAggregate, ...] = field(init=False)
    expiry_exposure: tuple[ExposureAggregate, ...] = field(init=False)
    stress_pnl_by_currency: tuple[StressPnLAggregate, ...] = field(init=False)
    expiry_concentration_by_currency: tuple[ExpiryConcentration, ...] = field(
        init=False
    )
    total_capital_use_seconds: Decimal = field(init=False)
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise DerivativeResearchError("portfolio_snapshot_schema_unsupported")
        require_stable_id(self.snapshot_id, "portfolio_snapshot.snapshot_id")
        snapshot_time = parse_timestamp(self.as_of, "portfolio_snapshot.as_of")
        require_hash(
            self.aggregation_policy_hash,
            "portfolio_snapshot.aggregation_policy_hash",
        )
        if self.aggregation_policy_hash != PORTFOLIO_EXPOSURE_AGGREGATION_POLICY_HASH:
            raise DerivativeResearchError(
                "portfolio_snapshot_aggregation_policy_unsupported"
            )
        if not isinstance(self.positions, tuple) or not self.positions:
            raise DerivativeResearchError("portfolio_snapshot_positions_tuple_required")
        if any(not isinstance(item, PositionExposure) for item in self.positions):
            raise DerivativeResearchError("portfolio_snapshot_position_invalid")
        position_ids = [item.position_id for item in self.positions]
        instrument_ids = [item.instrument_id for item in self.positions]
        position_hashes = [item.content_hash for item in self.positions]
        if len(set(position_ids)) != len(position_ids):
            raise DerivativeResearchError("portfolio_snapshot_position_duplicate")
        if len(set(instrument_ids)) != len(instrument_ids):
            raise DerivativeResearchError("portfolio_snapshot_instrument_duplicate")
        if len(set(position_hashes)) != len(position_hashes):
            raise DerivativeResearchError("portfolio_snapshot_position_hash_duplicate")
        for item in self.positions:
            if parse_timestamp(item.as_of, "portfolio_snapshot.position_as_of") != (
                snapshot_time
            ):
                raise DerivativeResearchError("portfolio_snapshot_as_of_mismatch")

        ordered_positions = tuple(
            sorted(self.positions, key=lambda item: item.position_id)
        )
        object.__setattr__(self, "positions", ordered_positions)
        self._require_complete_stress_scenarios()

        currencies = sorted({item.currency for item in ordered_positions})
        currency_exposure = tuple(
            _aggregate_positions(
                group=ExposureGroup.CURRENCY,
                group_value=currency,
                currency=currency,
                positions=[
                    item for item in ordered_positions if item.currency == currency
                ],
            )
            for currency in currencies
        )

        underlying_keys = sorted(
            {(item.underlying_id, item.currency) for item in ordered_positions}
        )
        underlying_exposure = tuple(
            _aggregate_positions(
                group=ExposureGroup.UNDERLYING,
                group_value=underlying_id,
                currency=currency,
                positions=[
                    item
                    for item in ordered_positions
                    if item.underlying_id == underlying_id and item.currency == currency
                ],
            )
            for underlying_id, currency in underlying_keys
        )

        expiry_keys = sorted(
            {
                (
                    _canonical_timestamp(
                        item.expiry_at,
                        "portfolio_snapshot.position_expiry",
                    ),
                    item.currency,
                )
                for item in ordered_positions
                if item.expiry_at is not None
            }
        )
        expiry_exposure = tuple(
            _aggregate_positions(
                group=ExposureGroup.EXPIRY,
                group_value=expiry_at,
                currency=currency,
                positions=[
                    item
                    for item in ordered_positions
                    if item.currency == currency
                    and item.expiry_at is not None
                    and _canonical_timestamp(
                        item.expiry_at,
                        "portfolio_snapshot.position_expiry",
                    )
                    == expiry_at
                ],
            )
            for expiry_at, currency in expiry_keys
        )

        stress_totals = self._build_stress_totals(currencies)
        concentration = tuple(
            self._build_expiry_concentration(currency) for currency in currencies
        )
        total_capital_seconds = _decimal_sum(
            [item.capital_use_seconds for item in ordered_positions]
        )

        object.__setattr__(self, "currency_exposure", currency_exposure)
        object.__setattr__(self, "underlying_exposure", underlying_exposure)
        object.__setattr__(self, "expiry_exposure", expiry_exposure)
        object.__setattr__(self, "stress_pnl_by_currency", stress_totals)
        object.__setattr__(
            self,
            "expiry_concentration_by_currency",
            concentration,
        )
        object.__setattr__(
            self,
            "total_capital_use_seconds",
            total_capital_seconds,
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="portfolio_exposure_snapshot",
            ),
        )

    def _require_complete_stress_scenarios(self) -> None:
        first = self.positions[0]
        expected = {item.scenario_id: item.scenario_hash for item in first.stress_pnl}
        for position in self.positions[1:]:
            observed = {
                item.scenario_id: item.scenario_hash for item in position.stress_pnl
            }
            if observed != expected:
                raise DerivativeResearchError(
                    "portfolio_snapshot_stress_scenario_set_mismatch"
                )

    def _build_stress_totals(
        self,
        currencies: Sequence[str],
    ) -> tuple[StressPnLAggregate, ...]:
        scenario_definitions = {
            item.scenario_id: item.scenario_hash
            for item in self.positions[0].stress_pnl
        }
        totals: list[StressPnLAggregate] = []
        for scenario_id in sorted(scenario_definitions):
            for currency in currencies:
                currency_positions = [
                    item for item in self.positions if item.currency == currency
                ]
                pnl_values = [
                    next(
                        stress.pnl
                        for stress in position.stress_pnl
                        if stress.scenario_id == scenario_id
                    )
                    for position in currency_positions
                ]
                totals.append(
                    StressPnLAggregate(
                        scenario_id=scenario_id,
                        scenario_hash=scenario_definitions[scenario_id],
                        currency=currency,
                        pnl=_decimal_sum(pnl_values),
                        member_hashes=tuple(
                            sorted(item.content_hash for item in currency_positions)
                        ),
                    )
                )
        return tuple(totals)

    def _build_expiry_concentration(self, currency: str) -> ExpiryConcentration:
        expiring_positions = [
            item
            for item in self.positions
            if item.currency == currency and item.expiry_at is not None
        ]
        basis_by_expiry: dict[str, Decimal] = {}
        for position in expiring_positions:
            expiry_value = position.expiry_at
            if expiry_value is None:  # narrowed defensively for static verification
                raise DerivativeResearchError(
                    "portfolio_snapshot_expiring_position_missing_expiry"
                )
            expiry = _canonical_timestamp(
                expiry_value,
                "portfolio_snapshot.position_expiry",
            )
            position_basis = (
                abs(position.premium)
                + position.margin_required
                + position.collateral_cash
            )
            basis_by_expiry[expiry] = basis_by_expiry.get(expiry, _ZERO) + (
                position_basis
            )
        total = _decimal_sum(list(basis_by_expiry.values()))
        if total == _ZERO:
            reason = (
                "no_expiring_positions"
                if not expiring_positions
                else "zero_expiry_capital_basis"
            )
            return ExpiryConcentration(
                currency=currency,
                total_capital_basis=_ZERO,
                shares=(),
                maximum_share=None,
                herfindahl_index=None,
                unavailable_reason=reason,
            )
        shares = tuple(
            ExpiryCapitalShare(
                expiry_at=expiry,
                capital_basis=basis_by_expiry[expiry],
                share=basis_by_expiry[expiry] / total,
            )
            for expiry in sorted(basis_by_expiry)
        )
        maximum = max(item.share for item in shares)
        herfindahl = _decimal_sum([item.share * item.share for item in shares])
        return ExpiryConcentration(
            currency=currency,
            total_capital_basis=total,
            shares=shares,
            maximum_share=maximum,
            herfindahl_index=herfindahl,
            unavailable_reason=None,
        )

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(item.currency for item in self.currency_exposure)

    @property
    def total_premium_by_currency(self) -> tuple[tuple[str, Decimal], ...]:
        return tuple((item.currency, item.premium) for item in self.currency_exposure)

    @property
    def total_margin_by_currency(self) -> tuple[tuple[str, Decimal], ...]:
        return tuple(
            (item.currency, item.margin_required) for item in self.currency_exposure
        )

    @property
    def total_collateral_by_currency(self) -> tuple[tuple[str, Decimal], ...]:
        return tuple(
            (item.currency, item.collateral_cash) for item in self.currency_exposure
        )

    @property
    def total_capital_use_seconds_by_currency(
        self,
    ) -> tuple[tuple[str, Decimal], ...]:
        return tuple(
            (item.currency, item.capital_use_seconds) for item in self.currency_exposure
        )

    def currency_total(self, currency: str) -> ExposureAggregate:
        _require_currency(currency, "portfolio_snapshot.currency_lookup")
        for item in self.currency_exposure:
            if item.currency == currency:
                return item
        raise DerivativeResearchError("portfolio_snapshot_currency_not_found")

    def monetary_total(self, field_name: str, currency: str | None = None) -> Decimal:
        """Return one monetary total without ever performing implicit FX."""

        fields = {
            "premium": "premium",
            "margin_required": "margin_required",
            "collateral_cash": "collateral_cash",
        }
        attribute = fields.get(field_name)
        if attribute is None:
            raise DerivativeResearchError("portfolio_snapshot_monetary_field_unknown")
        if currency is None:
            if len(self.currency_exposure) != 1:
                raise DerivativeResearchError(
                    "portfolio_snapshot_fx_conversion_required"
                )
            aggregate = self.currency_exposure[0]
        else:
            aggregate = self.currency_total(currency)
        value = getattr(aggregate, attribute)
        if not isinstance(value, Decimal):
            raise DerivativeResearchError("portfolio_snapshot_monetary_total_invalid")
        return value

    def stress_total(self, scenario_id: str, currency: str | None = None) -> Decimal:
        require_stable_id(scenario_id, "portfolio_snapshot.stress_scenario_lookup")
        if currency is None:
            if len(self.currency_exposure) != 1:
                raise DerivativeResearchError(
                    "portfolio_snapshot_fx_conversion_required"
                )
            currency = self.currency_exposure[0].currency
        _require_currency(currency, "portfolio_snapshot.stress_currency_lookup")
        for item in self.stress_pnl_by_currency:
            if item.scenario_id == scenario_id and item.currency == currency:
                return item.pnl
        raise DerivativeResearchError("portfolio_snapshot_stress_total_not_found")

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of,
            "aggregation_policy": PORTFOLIO_EXPOSURE_AGGREGATION_POLICY,
            "aggregation_policy_hash": self.aggregation_policy_hash,
            "positions": [item.as_dict() for item in self.positions],
            "currency_exposure": [item.as_dict() for item in self.currency_exposure],
            "underlying_exposure": [
                item.as_dict() for item in self.underlying_exposure
            ],
            "expiry_exposure": [item.as_dict() for item in self.expiry_exposure],
            "stress_pnl_by_currency": [
                item.as_dict() for item in self.stress_pnl_by_currency
            ],
            "expiry_concentration_by_currency": [
                item.as_dict() for item in self.expiry_concentration_by_currency
            ],
            "total_capital_use_seconds": decimal_text(self.total_capital_use_seconds),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}
