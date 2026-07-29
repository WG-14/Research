"""Versioned futures master, delivery, CTD, and margin research authority.

This module consumes externally prepared immutable terms and market snapshots.
It is a deterministic simulator: it has no exchange, account, order-routing,
or operational margin capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Sequence

from ..hashing import sha256_prefixed


FUTURES_DELIVERY_SCHEMA_VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class FuturesDeliveryError(ValueError):
    """Futures terms or lifecycle evidence is incomplete or inconsistent."""


class FuturesSettlementMode(StrEnum):
    CASH = "CASH"
    PHYSICAL = "PHYSICAL"


class RollAdjustmentMethod(StrEnum):
    NONE = "NONE"
    DIFFERENCE = "DIFFERENCE"
    RATIO = "RATIO"
    FIXED_MATURITY = "FIXED_MATURITY"
    WEIGHTED_MATURITY = "WEIGHTED_MATURITY"


class FuturesLifecycleEventType(StrEnum):
    NOTICE = "NOTICE"
    ASSIGNMENT = "ASSIGNMENT"
    TENDER = "TENDER"
    DELIVERY = "DELIVERY"
    CASH_SETTLEMENT = "CASH_SETTLEMENT"
    DEFAULT = "DEFAULT"
    CLOSE_OUT = "CLOSE_OUT"


class CollateralAssetKind(StrEnum):
    CASH = "CASH"
    GOVERNMENT_SECURITY = "GOVERNMENT_SECURITY"


def _require_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise FuturesDeliveryError(f"{field}_invalid")


def _require_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise FuturesDeliveryError(f"{field}_invalid_hash")


def _require_currency(value: str, field: str) -> None:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise FuturesDeliveryError(f"{field}_invalid_currency")


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise FuturesDeliveryError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FuturesDeliveryError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FuturesDeliveryError(f"{field}_invalid_date") from exc


def _decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, (bool, float)):
        raise FuturesDeliveryError(f"{field}_exact_decimal_required")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FuturesDeliveryError(f"{field}_exact_decimal_required") from exc
    if (
        not parsed.is_finite()
        or (positive and parsed <= _ZERO)
        or (nonnegative and parsed < _ZERO)
    ):
        raise FuturesDeliveryError(f"{field}_invalid_decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


@dataclass(frozen=True, slots=True)
class FuturesTermsMetadata:
    valid_from: str
    valid_to: str | None
    knowledge_at: str
    source_id: str
    source_version: str
    source_hash: str
    policy_version: str

    def __post_init__(self) -> None:
        start = _timestamp(self.valid_from, "terms.valid_from")
        cutoff = _timestamp(self.knowledge_at, "terms.knowledge_at")
        if (
            self.valid_to is not None
            and _timestamp(self.valid_to, "terms.valid_to") <= start
        ):
            raise FuturesDeliveryError("terms_validity_invalid")
        if cutoff < start:
            # Future-effective terms may be published before their start, but a
            # correction cannot claim it was known before the immutable source.
            pass
        for field in ("source_id", "source_version", "policy_version"):
            _require_id(getattr(self, field), f"terms.{field}")
        _require_hash(self.source_hash, "terms.source_hash")

    def effective_at(self, value: str) -> bool:
        query = _timestamp(value, "terms.query_at")
        start = _timestamp(self.valid_from, "terms.valid_from")
        end = (
            _timestamp(self.valid_to, "terms.valid_to")
            if self.valid_to is not None
            else None
        )
        return start <= query and (end is None or query < end)

    def known_at(self, value: str) -> bool:
        return _timestamp(self.knowledge_at, "terms.knowledge_at") <= _timestamp(
            value, "terms.query_knowledge_at"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "knowledge_at": self.knowledge_at,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "source_hash": self.source_hash,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class FuturesContractMasterVersion:
    record_id: str
    version: int
    contract_id: str
    root_id: str
    economic_underlying_id: str
    exchange_mic: str
    contract_month: str
    listed_at: str
    last_trade_at: str
    first_notice_at: str | None
    final_settlement_at: str
    delivery_start_at: str | None
    delivery_end_at: str | None
    contract_multiplier: Decimal
    contract_unit: str
    minimum_tick: Decimal
    tick_value: Decimal
    trading_currency: str
    settlement_currency: str
    settlement_mode: FuturesSettlementMode
    settlement_formula_id: str
    calendar_id: str
    session_id: str
    daily_price_limit: Decimal | None
    margin_policy_id: str
    deliverable_basket_id: str | None
    metadata: FuturesTermsMetadata
    supersedes_hash: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "record_id",
            "contract_id",
            "root_id",
            "economic_underlying_id",
            "contract_unit",
            "settlement_formula_id",
            "calendar_id",
            "session_id",
            "margin_policy_id",
        ):
            _require_id(getattr(self, field), f"contract_master.{field}")
        if not re.fullmatch(r"[A-Z0-9]{4}", self.exchange_mic):
            raise FuturesDeliveryError("contract_master.exchange_mic_invalid")
        if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", self.contract_month):
            raise FuturesDeliveryError("contract_master.contract_month_invalid")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise FuturesDeliveryError("contract_master.version_invalid")
        listed = _timestamp(self.listed_at, "contract_master.listed_at")
        last_trade = _timestamp(self.last_trade_at, "contract_master.last_trade_at")
        settlement = _timestamp(
            self.final_settlement_at,
            "contract_master.final_settlement_at",
        )
        if not listed < last_trade <= settlement:
            raise FuturesDeliveryError("contract_master_trade_dates_invalid")
        notice = (
            _timestamp(self.first_notice_at, "contract_master.first_notice_at")
            if self.first_notice_at is not None
            else None
        )
        delivery_start = (
            _timestamp(
                self.delivery_start_at,
                "contract_master.delivery_start_at",
            )
            if self.delivery_start_at is not None
            else None
        )
        delivery_end = (
            _timestamp(
                self.delivery_end_at,
                "contract_master.delivery_end_at",
            )
            if self.delivery_end_at is not None
            else None
        )
        for field in ("contract_multiplier", "minimum_tick", "tick_value"):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"contract_master.{field}",
                    positive=True,
                ),
            )
        if self.tick_value != self.minimum_tick * self.contract_multiplier:
            raise FuturesDeliveryError("contract_master_tick_value_mismatch")
        for field in ("trading_currency", "settlement_currency"):
            _require_currency(getattr(self, field), f"contract_master.{field}")
        if not isinstance(self.settlement_mode, FuturesSettlementMode):
            raise FuturesDeliveryError("contract_master.settlement_mode_invalid")
        if self.daily_price_limit is not None:
            object.__setattr__(
                self,
                "daily_price_limit",
                _decimal(
                    self.daily_price_limit,
                    "contract_master.daily_price_limit",
                    positive=True,
                ),
            )
        if self.settlement_mode is FuturesSettlementMode.PHYSICAL:
            if (
                notice is None
                or delivery_start is None
                or delivery_end is None
                or self.deliverable_basket_id is None
                or not notice <= delivery_start <= delivery_end
                or delivery_end < last_trade
            ):
                raise FuturesDeliveryError(
                    "physical_contract_delivery_terms_incomplete"
                )
            _require_id(
                self.deliverable_basket_id,
                "contract_master.deliverable_basket_id",
            )
        elif any(
            value is not None
            for value in (
                notice,
                delivery_start,
                delivery_end,
                self.deliverable_basket_id,
            )
        ):
            raise FuturesDeliveryError("cash_contract_physical_terms_forbidden")
        if self.version == 1 and self.supersedes_hash is not None:
            raise FuturesDeliveryError("initial_contract_version_cannot_supersede")
        if self.version > 1:
            if self.supersedes_hash is None:
                raise FuturesDeliveryError("contract_revision_supersedes_required")
            _require_hash(
                self.supersedes_hash,
                "contract_master.supersedes_hash",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_DELIVERY_SCHEMA_VERSION,
            "record_id": self.record_id,
            "version": self.version,
            "contract_id": self.contract_id,
            "root_id": self.root_id,
            "economic_underlying_id": self.economic_underlying_id,
            "exchange_mic": self.exchange_mic,
            "contract_month": self.contract_month,
            "listed_at": self.listed_at,
            "last_trade_at": self.last_trade_at,
            "first_notice_at": self.first_notice_at,
            "final_settlement_at": self.final_settlement_at,
            "delivery_start_at": self.delivery_start_at,
            "delivery_end_at": self.delivery_end_at,
            "contract_multiplier": _decimal_text(self.contract_multiplier),
            "contract_unit": self.contract_unit,
            "minimum_tick": _decimal_text(self.minimum_tick),
            "tick_value": _decimal_text(self.tick_value),
            "trading_currency": self.trading_currency,
            "settlement_currency": self.settlement_currency,
            "settlement_mode": self.settlement_mode.value,
            "settlement_formula_id": self.settlement_formula_id,
            "calendar_id": self.calendar_id,
            "session_id": self.session_id,
            "daily_price_limit": (
                _decimal_text(self.daily_price_limit)
                if self.daily_price_limit is not None
                else None
            ),
            "margin_policy_id": self.margin_policy_id,
            "deliverable_basket_id": self.deliverable_basket_id,
            "metadata": self.metadata.as_dict(),
            "supersedes_hash": self.supersedes_hash,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="futures_contract_master")

    def tradable_at(self, value: str) -> bool:
        query = _timestamp(value, "contract_master.query_at")
        return (
            _timestamp(self.listed_at, "contract_master.listed_at")
            <= query
            < _timestamp(self.last_trade_at, "contract_master.last_trade_at")
        )


@dataclass(frozen=True, slots=True)
class FuturesContractMasterHistory:
    versions: tuple[FuturesContractMasterVersion, ...]

    def __post_init__(self) -> None:
        if not self.versions:
            raise FuturesDeliveryError("contract_master_history_empty")
        histories: dict[str, list[FuturesContractMasterVersion]] = {}
        for item in self.versions:
            histories.setdefault(item.record_id, []).append(item)
        for record_id, versions in histories.items():
            ordered = sorted(versions, key=lambda item: item.version)
            if [item.version for item in ordered] != list(range(1, len(ordered) + 1)):
                raise FuturesDeliveryError(
                    f"contract_master_revision_not_contiguous:{record_id}"
                )
            for previous, current in zip(ordered, ordered[1:]):
                if current.contract_id != previous.contract_id:
                    raise FuturesDeliveryError(
                        "contract_master_revision_contract_changed"
                    )
                if current.supersedes_hash != previous.contract_hash():
                    raise FuturesDeliveryError("contract_master_revision_chain_broken")
                if _timestamp(
                    current.metadata.knowledge_at,
                    "contract_master.knowledge_at",
                ) <= _timestamp(
                    previous.metadata.knowledge_at,
                    "contract_master.previous_knowledge_at",
                ):
                    raise FuturesDeliveryError(
                        "contract_master_knowledge_not_increasing"
                    )

    def resolve(
        self,
        contract_id: str,
        *,
        valid_at: str,
        knowledge_at: str,
    ) -> FuturesContractMasterVersion:
        latest: dict[str, FuturesContractMasterVersion] = {}
        for item in self.versions:
            if (
                item.contract_id != contract_id
                or not item.metadata.effective_at(valid_at)
                or not item.metadata.known_at(knowledge_at)
            ):
                continue
            prior = latest.get(item.record_id)
            if prior is None or item.version > prior.version:
                latest[item.record_id] = item
        if len(latest) != 1:
            raise FuturesDeliveryError(
                f"contract_master_not_unique_as_of:{contract_id}"
            )
        return next(iter(latest.values()))

    def content_hash(self) -> str:
        return sha256_prefixed(
            {
                "versions": [
                    item.as_dict()
                    for item in sorted(
                        self.versions,
                        key=lambda item: (item.record_id, item.version),
                    )
                ]
            },
            label="futures_contract_master_history",
        )


@dataclass(frozen=True, slots=True)
class ContinuousSeriesManifest:
    series_id: str
    root_id: str
    source_contract_ids: tuple[str, ...]
    roll_rule_id: str
    roll_window_days: int
    liquidity_rule_id: str
    delivery_avoidance_rule_id: str
    adjustment_method: RollAdjustmentMethod
    adjustment_values: tuple[tuple[str, Decimal], ...]
    builder_version: str
    source_snapshot_hashes: tuple[str, ...]
    generated_series_hash: str
    generated_at: str
    signal_only: bool = True

    def __post_init__(self) -> None:
        for field in (
            "series_id",
            "root_id",
            "roll_rule_id",
            "liquidity_rule_id",
            "delivery_avoidance_rule_id",
            "builder_version",
        ):
            _require_id(getattr(self, field), f"continuous_manifest.{field}")
        if not self.source_contract_ids or self.source_contract_ids != tuple(
            sorted(set(self.source_contract_ids))
        ):
            raise FuturesDeliveryError(
                "continuous_manifest_source_contracts_not_canonical"
            )
        for item in self.source_contract_ids:
            _require_id(item, "continuous_manifest.source_contract_id")
        if (
            isinstance(self.roll_window_days, bool)
            or not isinstance(self.roll_window_days, int)
            or self.roll_window_days < 1
        ):
            raise FuturesDeliveryError("continuous_manifest.roll_window_days_invalid")
        if not isinstance(self.adjustment_method, RollAdjustmentMethod):
            raise FuturesDeliveryError("continuous_manifest.adjustment_method_invalid")
        keys = [item[0] for item in self.adjustment_values]
        if keys != sorted(set(keys)):
            raise FuturesDeliveryError(
                "continuous_manifest_adjustment_values_not_canonical"
            )
        for contract_id, value in self.adjustment_values:
            if contract_id not in self.source_contract_ids:
                raise FuturesDeliveryError(
                    "continuous_manifest_adjustment_contract_unknown"
                )
            _decimal(value, "continuous_manifest.adjustment_value")
        if not self.source_snapshot_hashes or self.source_snapshot_hashes != tuple(
            sorted(set(self.source_snapshot_hashes))
        ):
            raise FuturesDeliveryError(
                "continuous_manifest_source_hashes_not_canonical"
            )
        for hash_value in (
            *self.source_snapshot_hashes,
            self.generated_series_hash,
        ):
            _require_hash(hash_value, "continuous_manifest.hash")
        _timestamp(self.generated_at, "continuous_manifest.generated_at")
        if not self.signal_only:
            raise FuturesDeliveryError("continuous_manifest_must_be_signal_only")

    def as_dict(self) -> dict[str, object]:
        return {
            "series_id": self.series_id,
            "root_id": self.root_id,
            "source_contract_ids": list(self.source_contract_ids),
            "roll_rule_id": self.roll_rule_id,
            "roll_window_days": self.roll_window_days,
            "liquidity_rule_id": self.liquidity_rule_id,
            "delivery_avoidance_rule_id": self.delivery_avoidance_rule_id,
            "adjustment_method": self.adjustment_method.value,
            "adjustment_values": [
                {"contract_id": key, "value": _decimal_text(value)}
                for key, value in self.adjustment_values
            ],
            "builder_version": self.builder_version,
            "source_snapshot_hashes": list(self.source_snapshot_hashes),
            "generated_series_hash": self.generated_series_hash,
            "generated_at": self.generated_at,
            "signal_only": self.signal_only,
        }

    def manifest_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="continuous_futures_manifest")

    def require_actual_contract(self, identifier: str) -> str:
        if identifier == self.series_id:
            raise FuturesDeliveryError("continuous_series_not_tradable")
        if identifier not in self.source_contract_ids:
            raise FuturesDeliveryError("contract_not_bound_to_continuous_manifest")
        return identifier


@dataclass(frozen=True, slots=True)
class RollYieldPolicy:
    policy_id: str
    version: str
    definition: str = "price_return_difference_between_expiring_and_replacement"
    cash_pnl_is_separate: bool = True

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "roll_yield_policy.policy_id")
        _require_id(self.version, "roll_yield_policy.version")
        if self.definition not in {
            "price_return_difference_between_expiring_and_replacement",
            "annualized_log_price_spread",
        }:
            raise FuturesDeliveryError("roll_yield_policy.definition_unknown")
        if not self.cash_pnl_is_separate:
            raise FuturesDeliveryError("roll_yield_must_be_separate_from_cash_pnl")

    def calculate(
        self,
        *,
        expiring_price: Decimal,
        replacement_price: Decimal,
        days: int,
    ) -> Decimal:
        old = _decimal(expiring_price, "roll_yield.expiring_price", positive=True)
        new = _decimal(replacement_price, "roll_yield.replacement_price", positive=True)
        if isinstance(days, bool) or days < 1:
            raise FuturesDeliveryError("roll_yield.days_invalid")
        spread = (old - new) / old
        if self.definition == "annualized_log_price_spread":
            # A deterministic exact proxy avoids binary floating-point logs.
            spread = spread * Decimal("365") / Decimal(days)
        return spread

    def policy_hash(self) -> str:
        return sha256_prefixed(
            {
                "policy_id": self.policy_id,
                "version": self.version,
                "definition": self.definition,
                "cash_pnl_is_separate": self.cash_pnl_is_separate,
            },
            label="futures_roll_yield_policy",
        )


@dataclass(frozen=True, slots=True)
class FuturesSelectionCandidate:
    contract: FuturesContractMasterVersion
    observed_at: str
    knowledge_at: str
    bid: Decimal
    ask: Decimal
    settlement_price: Decimal
    volume: Decimal
    open_interest: Decimal
    source_quote_hash: str

    def __post_init__(self) -> None:
        observed = _timestamp(self.observed_at, "candidate.observed_at")
        known = _timestamp(self.knowledge_at, "candidate.knowledge_at")
        if known < observed:
            raise FuturesDeliveryError("candidate_knowledge_before_observation")
        for field in (
            "bid",
            "ask",
            "settlement_price",
            "volume",
            "open_interest",
        ):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"candidate.{field}",
                    positive=field in {"bid", "ask", "settlement_price"},
                    nonnegative=field in {"volume", "open_interest"},
                ),
            )
        if self.ask < self.bid:
            raise FuturesDeliveryError("candidate_crossed_quote")
        _require_hash(self.source_quote_hash, "candidate.source_quote_hash")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    def candidate_hash(self) -> str:
        return sha256_prefixed(
            {
                "contract_hash": self.contract.contract_hash(),
                "observed_at": self.observed_at,
                "knowledge_at": self.knowledge_at,
                "bid": _decimal_text(self.bid),
                "ask": _decimal_text(self.ask),
                "settlement_price": _decimal_text(self.settlement_price),
                "volume": _decimal_text(self.volume),
                "open_interest": _decimal_text(self.open_interest),
                "source_quote_hash": self.source_quote_hash,
            },
            label="futures_selection_candidate",
        )


@dataclass(frozen=True, slots=True)
class ContractSelectionPolicy:
    policy_id: str
    version: str
    minimum_days_to_notice: int
    minimum_days_to_last_trade: int
    minimum_volume: Decimal
    minimum_open_interest: Decimal
    maximum_spread: Decimal
    volume_weight: Decimal = Decimal("1")
    open_interest_weight: Decimal = Decimal("1")
    spread_penalty_weight: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "selection_policy.policy_id")
        _require_id(self.version, "selection_policy.version")
        for field in ("minimum_days_to_notice", "minimum_days_to_last_trade"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FuturesDeliveryError(f"selection_policy.{field}_invalid")
        for field in (
            "minimum_volume",
            "minimum_open_interest",
            "maximum_spread",
            "volume_weight",
            "open_interest_weight",
            "spread_penalty_weight",
        ):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"selection_policy.{field}",
                    nonnegative=True,
                ),
            )

    def policy_hash(self) -> str:
        return sha256_prefixed(
            {
                "policy_id": self.policy_id,
                "version": self.version,
                "minimum_days_to_notice": self.minimum_days_to_notice,
                "minimum_days_to_last_trade": self.minimum_days_to_last_trade,
                "minimum_volume": _decimal_text(self.minimum_volume),
                "minimum_open_interest": _decimal_text(self.minimum_open_interest),
                "maximum_spread": _decimal_text(self.maximum_spread),
                "volume_weight": _decimal_text(self.volume_weight),
                "open_interest_weight": _decimal_text(self.open_interest_weight),
                "spread_penalty_weight": _decimal_text(self.spread_penalty_weight),
            },
            label="futures_contract_selection_policy",
        )


@dataclass(frozen=True, slots=True)
class ContractSelectionReceipt:
    decision_at: str
    knowledge_at: str
    selected_contract_id: str
    selected_contract_hash: str
    selected_quote_hash: str
    selected_score: Decimal
    candidate_hashes: tuple[str, ...]
    rejected: tuple[tuple[str, tuple[str, ...]], ...]
    policy_hash: str

    def __post_init__(self) -> None:
        _timestamp(self.decision_at, "selection_receipt.decision_at")
        _timestamp(self.knowledge_at, "selection_receipt.knowledge_at")
        _require_id(
            self.selected_contract_id,
            "selection_receipt.selected_contract_id",
        )
        for value in (
            self.selected_contract_hash,
            self.selected_quote_hash,
            self.policy_hash,
            *self.candidate_hashes,
        ):
            _require_hash(value, "selection_receipt.hash")

    def receipt_hash(self) -> str:
        return sha256_prefixed(
            {
                "decision_at": self.decision_at,
                "knowledge_at": self.knowledge_at,
                "selected_contract_id": self.selected_contract_id,
                "selected_contract_hash": self.selected_contract_hash,
                "selected_quote_hash": self.selected_quote_hash,
                "selected_score": _decimal_text(self.selected_score),
                "candidate_hashes": list(self.candidate_hashes),
                "rejected": [
                    {"contract_id": key, "reasons": list(value)}
                    for key, value in self.rejected
                ],
                "policy_hash": self.policy_hash,
            },
            label="futures_contract_selection_receipt",
        )


def select_actual_contract(
    candidates: Sequence[FuturesSelectionCandidate],
    *,
    root_id: str,
    decision_at: str,
    knowledge_at: str,
    policy: ContractSelectionPolicy,
) -> ContractSelectionReceipt:
    """Select an actual listed contract from a point-in-time chain."""

    _require_id(root_id, "selection.root_id")
    decision = _timestamp(decision_at, "selection.decision_at")
    knowledge = _timestamp(knowledge_at, "selection.knowledge_at")
    if knowledge > decision:
        raise FuturesDeliveryError("selection_future_knowledge")
    if not candidates:
        raise FuturesDeliveryError("selection_candidates_required")
    if len({item.contract.contract_id for item in candidates}) != len(candidates):
        raise FuturesDeliveryError("selection_contract_duplicate")
    eligible: list[tuple[Decimal, FuturesSelectionCandidate]] = []
    rejected: list[tuple[str, tuple[str, ...]]] = []
    for candidate in candidates:
        contract = candidate.contract
        reasons: list[str] = []
        if contract.root_id != root_id:
            reasons.append("ROOT_MISMATCH")
        if _timestamp(candidate.knowledge_at, "candidate.knowledge_at") > knowledge:
            reasons.append("FUTURE_KNOWLEDGE")
        if not contract.tradable_at(decision_at):
            reasons.append("NOT_TRADABLE")
        if candidate.volume < policy.minimum_volume:
            reasons.append("VOLUME")
        if candidate.open_interest < policy.minimum_open_interest:
            reasons.append("OPEN_INTEREST")
        if candidate.spread > policy.maximum_spread:
            reasons.append("SPREAD")
        last_trade_days = (
            _timestamp(
                contract.last_trade_at,
                "contract_master.last_trade_at",
            ).date()
            - decision.date()
        ).days
        if last_trade_days < policy.minimum_days_to_last_trade:
            reasons.append("LAST_TRADE")
        if contract.first_notice_at is not None:
            notice_days = (
                _timestamp(
                    contract.first_notice_at,
                    "contract_master.first_notice_at",
                ).date()
                - decision.date()
            ).days
            if notice_days < policy.minimum_days_to_notice:
                reasons.append("FIRST_NOTICE")
        if reasons:
            rejected.append((contract.contract_id, tuple(sorted(reasons))))
            continue
        score = (
            candidate.volume * policy.volume_weight
            + candidate.open_interest * policy.open_interest_weight
            - candidate.spread * policy.spread_penalty_weight
        )
        eligible.append((score, candidate))
    if not eligible:
        raise FuturesDeliveryError("selection_no_eligible_actual_contract")
    score, selected = max(
        eligible,
        key=lambda item: (
            item[0],
            _timestamp(
                item[1].contract.last_trade_at,
                "contract_master.last_trade_at",
            ),
            item[1].contract.contract_id,
        ),
    )
    return ContractSelectionReceipt(
        decision_at=decision_at,
        knowledge_at=knowledge_at,
        selected_contract_id=selected.contract.contract_id,
        selected_contract_hash=selected.contract.contract_hash(),
        selected_quote_hash=selected.source_quote_hash,
        selected_score=score,
        candidate_hashes=tuple(sorted(item.candidate_hash() for item in candidates)),
        rejected=tuple(sorted(rejected)),
        policy_hash=policy.policy_hash(),
    )


@dataclass(frozen=True, slots=True)
class DeliverableGrade:
    grade_id: str
    instrument_id: str
    location_id: str
    clean_cash_price: Decimal
    accrued_interest: Decimal
    conversion_factor: Decimal
    quality_adjustment: Decimal
    location_adjustment: Decimal
    delivery_cost: Decimal
    source_hash: str

    def __post_init__(self) -> None:
        for field in ("grade_id", "instrument_id", "location_id"):
            _require_id(getattr(self, field), f"deliverable_grade.{field}")
        for field in (
            "clean_cash_price",
            "conversion_factor",
        ):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"deliverable_grade.{field}",
                    positive=True,
                ),
            )
        for field in (
            "accrued_interest",
            "delivery_cost",
        ):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"deliverable_grade.{field}",
                    nonnegative=True,
                ),
            )
        for field in ("quality_adjustment", "location_adjustment"):
            object.__setattr__(
                self,
                field,
                _decimal(getattr(self, field), f"deliverable_grade.{field}"),
            )
        _require_hash(self.source_hash, "deliverable_grade.source_hash")

    def grade_hash(self) -> str:
        return sha256_prefixed(
            {
                "grade_id": self.grade_id,
                "instrument_id": self.instrument_id,
                "location_id": self.location_id,
                "clean_cash_price": _decimal_text(self.clean_cash_price),
                "accrued_interest": _decimal_text(self.accrued_interest),
                "conversion_factor": _decimal_text(self.conversion_factor),
                "quality_adjustment": _decimal_text(self.quality_adjustment),
                "location_adjustment": _decimal_text(self.location_adjustment),
                "delivery_cost": _decimal_text(self.delivery_cost),
                "source_hash": self.source_hash,
            },
            label="futures_deliverable_grade",
        )


@dataclass(frozen=True, slots=True)
class DeliverableBasket:
    basket_id: str
    version: str
    contract_root_id: str
    delivery_unit: Decimal
    grades: tuple[DeliverableGrade, ...]
    valid_from: str
    valid_to: str | None
    knowledge_at: str
    source_hash: str

    def __post_init__(self) -> None:
        for field in ("basket_id", "version", "contract_root_id"):
            _require_id(getattr(self, field), f"deliverable_basket.{field}")
        object.__setattr__(
            self,
            "delivery_unit",
            _decimal(
                self.delivery_unit,
                "deliverable_basket.delivery_unit",
                positive=True,
            ),
        )
        identifiers = [item.grade_id for item in self.grades]
        if not identifiers or identifiers != sorted(set(identifiers)):
            raise FuturesDeliveryError("deliverable_basket_grades_not_unique_canonical")
        start = _timestamp(self.valid_from, "deliverable_basket.valid_from")
        if (
            self.valid_to is not None
            and _timestamp(self.valid_to, "deliverable_basket.valid_to") <= start
        ):
            raise FuturesDeliveryError("deliverable_basket_validity_invalid")
        _timestamp(self.knowledge_at, "deliverable_basket.knowledge_at")
        _require_hash(self.source_hash, "deliverable_basket.source_hash")

    def basket_hash(self) -> str:
        return sha256_prefixed(
            {
                "basket_id": self.basket_id,
                "version": self.version,
                "contract_root_id": self.contract_root_id,
                "delivery_unit": _decimal_text(self.delivery_unit),
                "grade_hashes": [item.grade_hash() for item in self.grades],
                "valid_from": self.valid_from,
                "valid_to": self.valid_to,
                "knowledge_at": self.knowledge_at,
                "source_hash": self.source_hash,
            },
            label="futures_deliverable_basket",
        )


@dataclass(frozen=True, slots=True)
class CTDComparison:
    grade_id: str
    instrument_id: str
    dirty_cash_cost: Decimal
    invoice_amount: Decimal
    net_basis: Decimal
    implied_delivery_gain: Decimal
    grade_hash: str


@dataclass(frozen=True, slots=True)
class CTDDecision:
    contract_id: str
    basket_id: str
    futures_settlement_price: Decimal
    selected_grade_id: str
    comparisons: tuple[CTDComparison, ...]
    contract_hash: str
    basket_hash: str

    def decision_hash(self) -> str:
        return sha256_prefixed(
            {
                "contract_id": self.contract_id,
                "basket_id": self.basket_id,
                "futures_settlement_price": _decimal_text(
                    self.futures_settlement_price
                ),
                "selected_grade_id": self.selected_grade_id,
                "comparisons": [
                    {
                        "grade_id": item.grade_id,
                        "instrument_id": item.instrument_id,
                        "dirty_cash_cost": _decimal_text(item.dirty_cash_cost),
                        "invoice_amount": _decimal_text(item.invoice_amount),
                        "net_basis": _decimal_text(item.net_basis),
                        "implied_delivery_gain": _decimal_text(
                            item.implied_delivery_gain
                        ),
                        "grade_hash": item.grade_hash,
                    }
                    for item in self.comparisons
                ],
                "contract_hash": self.contract_hash,
                "basket_hash": self.basket_hash,
            },
            label="futures_ctd_decision",
        )


def select_cheapest_to_deliver(
    contract: FuturesContractMasterVersion,
    basket: DeliverableBasket,
    *,
    futures_settlement_price: Decimal,
) -> CTDDecision:
    """Compare every grade economically; smallest net basis is the CTD."""

    if contract.settlement_mode is not FuturesSettlementMode.PHYSICAL:
        raise FuturesDeliveryError("ctd_not_applicable_to_cash_contract")
    if (
        contract.deliverable_basket_id != basket.basket_id
        or contract.root_id != basket.contract_root_id
    ):
        raise FuturesDeliveryError("ctd_contract_basket_binding_mismatch")
    settlement = _decimal(
        futures_settlement_price,
        "ctd.futures_settlement_price",
        positive=True,
    )
    comparisons: list[CTDComparison] = []
    for grade in basket.grades:
        dirty = (
            grade.clean_cash_price
            + grade.accrued_interest
            + grade.quality_adjustment
            + grade.location_adjustment
        ) * basket.delivery_unit + grade.delivery_cost
        invoice = (
            settlement * grade.conversion_factor + grade.accrued_interest
        ) * basket.delivery_unit
        net_basis = dirty - invoice
        comparisons.append(
            CTDComparison(
                grade_id=grade.grade_id,
                instrument_id=grade.instrument_id,
                dirty_cash_cost=dirty,
                invoice_amount=invoice,
                net_basis=net_basis,
                implied_delivery_gain=-net_basis,
                grade_hash=grade.grade_hash(),
            )
        )
    comparisons.sort(key=lambda item: (item.net_basis, item.grade_id))
    return CTDDecision(
        contract_id=contract.contract_id,
        basket_id=basket.basket_id,
        futures_settlement_price=settlement,
        selected_grade_id=comparisons[0].grade_id,
        comparisons=tuple(comparisons),
        contract_hash=contract.contract_hash(),
        basket_hash=basket.basket_hash(),
    )


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    policy_id: str
    version: str
    physical_delivery_enabled: bool
    close_before_notice_days: int
    default_closeout_penalty_rate: Decimal

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "delivery_policy.policy_id")
        _require_id(self.version, "delivery_policy.version")
        if (
            isinstance(self.close_before_notice_days, bool)
            or not isinstance(self.close_before_notice_days, int)
            or self.close_before_notice_days < 0
        ):
            raise FuturesDeliveryError(
                "delivery_policy.close_before_notice_days_invalid"
            )
        object.__setattr__(
            self,
            "default_closeout_penalty_rate",
            _decimal(
                self.default_closeout_penalty_rate,
                "delivery_policy.default_closeout_penalty_rate",
                nonnegative=True,
            ),
        )

    def policy_hash(self) -> str:
        return sha256_prefixed(
            {
                "policy_id": self.policy_id,
                "version": self.version,
                "physical_delivery_enabled": self.physical_delivery_enabled,
                "close_before_notice_days": self.close_before_notice_days,
                "default_closeout_penalty_rate": _decimal_text(
                    self.default_closeout_penalty_rate
                ),
            },
            label="futures_delivery_policy",
        )


@dataclass(frozen=True, slots=True)
class FuturesLifecyclePosting:
    posting_id: str
    event_type: FuturesLifecycleEventType
    occurred_at: str
    contract_id: str
    contract_quantity_delta: Decimal
    cash_delta: Decimal
    currency: str
    delivered_instrument_id: str | None
    delivered_quantity_delta: Decimal
    source_hashes: tuple[str, ...]
    previous_posting_hash: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.posting_id, "lifecycle_posting.posting_id")
        _require_id(self.contract_id, "lifecycle_posting.contract_id")
        if not isinstance(self.event_type, FuturesLifecycleEventType):
            raise FuturesDeliveryError("lifecycle_posting.event_type_invalid")
        _timestamp(self.occurred_at, "lifecycle_posting.occurred_at")
        for field in (
            "contract_quantity_delta",
            "cash_delta",
            "delivered_quantity_delta",
        ):
            _decimal(getattr(self, field), f"lifecycle_posting.{field}")
        _require_currency(self.currency, "lifecycle_posting.currency")
        if self.delivered_instrument_id is not None:
            _require_id(
                self.delivered_instrument_id,
                "lifecycle_posting.delivered_instrument_id",
            )
        if (self.delivered_instrument_id is None) != (
            self.delivered_quantity_delta == _ZERO
        ):
            raise FuturesDeliveryError("lifecycle_posting_delivery_binding_invalid")
        if not self.source_hashes or self.source_hashes != tuple(
            sorted(set(self.source_hashes))
        ):
            raise FuturesDeliveryError("lifecycle_posting_source_hashes_not_canonical")
        for value in self.source_hashes:
            _require_hash(value, "lifecycle_posting.source_hash")
        if self.previous_posting_hash is not None:
            _require_hash(
                self.previous_posting_hash,
                "lifecycle_posting.previous_posting_hash",
            )

    def posting_hash(self) -> str:
        return sha256_prefixed(
            {
                "posting_id": self.posting_id,
                "event_type": self.event_type.value,
                "occurred_at": self.occurred_at,
                "contract_id": self.contract_id,
                "contract_quantity_delta": _decimal_text(self.contract_quantity_delta),
                "cash_delta": _decimal_text(self.cash_delta),
                "currency": self.currency,
                "delivered_instrument_id": self.delivered_instrument_id,
                "delivered_quantity_delta": _decimal_text(
                    self.delivered_quantity_delta
                ),
                "source_hashes": list(self.source_hashes),
                "previous_posting_hash": self.previous_posting_hash,
            },
            label="futures_lifecycle_posting",
        )


def settle_futures_position(
    contract: FuturesContractMasterVersion,
    *,
    quantity: Decimal,
    prior_settlement_price: Decimal,
    final_settlement_price: Decimal,
    occurred_at: str,
    policy: DeliveryPolicy,
    ctd: CTDDecision | None = None,
    defaulted: bool = False,
) -> tuple[FuturesLifecyclePosting, ...]:
    """Settle or deliver an open position using one common posting chain."""

    position = _decimal(quantity, "settlement.quantity")
    if position == _ZERO:
        raise FuturesDeliveryError("settlement_open_position_required")
    prior = _decimal(prior_settlement_price, "settlement.prior_price", positive=True)
    final = _decimal(final_settlement_price, "settlement.final_price", positive=True)
    when = _timestamp(occurred_at, "settlement.occurred_at")
    if when < _timestamp(
        contract.final_settlement_at,
        "contract_master.final_settlement_at",
    ):
        raise FuturesDeliveryError("settlement_before_final_settlement_time")
    variation = (final - prior) * contract.contract_multiplier * position
    base_sources = tuple(sorted((contract.contract_hash(), policy.policy_hash())))
    if defaulted:
        penalty = (
            abs(position)
            * final
            * contract.contract_multiplier
            * policy.default_closeout_penalty_rate
        )
        posting = FuturesLifecyclePosting(
            posting_id=f"{contract.contract_id}:default-close-out",
            event_type=FuturesLifecycleEventType.DEFAULT,
            occurred_at=occurred_at,
            contract_id=contract.contract_id,
            contract_quantity_delta=-position,
            cash_delta=variation - penalty,
            currency=contract.settlement_currency,
            delivered_instrument_id=None,
            delivered_quantity_delta=_ZERO,
            source_hashes=base_sources,
        )
        return (posting,)
    if contract.settlement_mode is FuturesSettlementMode.CASH:
        if ctd is not None:
            raise FuturesDeliveryError("cash_settlement_ctd_forbidden")
        posting = FuturesLifecyclePosting(
            posting_id=f"{contract.contract_id}:cash-settlement",
            event_type=FuturesLifecycleEventType.CASH_SETTLEMENT,
            occurred_at=occurred_at,
            contract_id=contract.contract_id,
            contract_quantity_delta=-position,
            cash_delta=variation,
            currency=contract.settlement_currency,
            delivered_instrument_id=None,
            delivered_quantity_delta=_ZERO,
            source_hashes=base_sources,
        )
        return (posting,)
    if not policy.physical_delivery_enabled:
        raise FuturesDeliveryError("physical_delivery_disabled_by_policy")
    if ctd is None or ctd.contract_hash != contract.contract_hash():
        raise FuturesDeliveryError("physical_settlement_ctd_required")
    selected = next(
        item for item in ctd.comparisons if item.grade_id == ctd.selected_grade_id
    )
    direction = _ONE if position > 0 else -_ONE
    invoice_cash = -direction * selected.invoice_amount * abs(position)
    posting = FuturesLifecyclePosting(
        posting_id=f"{contract.contract_id}:delivery",
        event_type=FuturesLifecycleEventType.DELIVERY,
        occurred_at=occurred_at,
        contract_id=contract.contract_id,
        contract_quantity_delta=-position,
        cash_delta=variation + invoice_cash,
        currency=contract.settlement_currency,
        delivered_instrument_id=selected.instrument_id,
        delivered_quantity_delta=(
            direction * contract.contract_multiplier * abs(position)
        ),
        source_hashes=tuple(
            sorted((*base_sources, ctd.decision_hash(), ctd.basket_hash))
        ),
    )
    return (posting,)


@dataclass(frozen=True, slots=True)
class CollateralEligibility:
    asset_id: str
    kind: CollateralAssetKind
    currency: str
    haircut: Decimal
    concentration_limit: Decimal
    source_hash: str

    def __post_init__(self) -> None:
        _require_id(self.asset_id, "collateral.asset_id")
        if not isinstance(self.kind, CollateralAssetKind):
            raise FuturesDeliveryError("collateral.kind_invalid")
        _require_currency(self.currency, "collateral.currency")
        for field in ("haircut", "concentration_limit"):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"collateral.{field}",
                    nonnegative=True,
                ),
            )
            if (field == "haircut" and getattr(self, field) >= _ONE) or (
                field == "concentration_limit" and getattr(self, field) > _ONE
            ):
                raise FuturesDeliveryError(f"collateral.{field}_out_of_range")
        _require_hash(self.source_hash, "collateral.source_hash")


@dataclass(frozen=True, slots=True)
class FuturesMarginPolicyVersion:
    policy_id: str
    version: int
    exchange_mic: str
    currency: str
    initial_per_contract: Decimal
    maintenance_per_contract: Decimal
    variation_frequency: str
    collateral: tuple[CollateralEligibility, ...]
    collateral_waterfall: tuple[str, ...]
    collateral_yield_rate: Decimal
    spread_offset_rate: Decimal
    additional_funding_allowed: bool
    metadata: FuturesTermsMetadata
    supersedes_hash: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "margin_policy.policy_id")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise FuturesDeliveryError("margin_policy.version_invalid")
        if not re.fullmatch(r"[A-Z0-9]{4}", self.exchange_mic):
            raise FuturesDeliveryError("margin_policy.exchange_mic_invalid")
        _require_currency(self.currency, "margin_policy.currency")
        for field in ("initial_per_contract", "maintenance_per_contract"):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"margin_policy.{field}",
                    positive=True,
                ),
            )
        if self.maintenance_per_contract > self.initial_per_contract:
            raise FuturesDeliveryError("margin_maintenance_exceeds_initial")
        if self.variation_frequency not in {"DAILY", "INTRADAY"}:
            raise FuturesDeliveryError("margin_variation_frequency_unknown")
        asset_ids = [item.asset_id for item in self.collateral]
        if asset_ids != sorted(set(asset_ids)):
            raise FuturesDeliveryError("margin_collateral_not_unique_canonical")
        if set(self.collateral_waterfall) != set(asset_ids) or len(
            self.collateral_waterfall
        ) != len(asset_ids):
            raise FuturesDeliveryError("margin_collateral_waterfall_invalid")
        for field in ("collateral_yield_rate", "spread_offset_rate"):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    f"margin_policy.{field}",
                    nonnegative=True,
                ),
            )
        if self.spread_offset_rate >= _ONE:
            raise FuturesDeliveryError("margin_spread_offset_invalid")
        if self.version == 1 and self.supersedes_hash is not None:
            raise FuturesDeliveryError("initial_margin_policy_supersedes_forbidden")
        if self.version > 1:
            if self.supersedes_hash is None:
                raise FuturesDeliveryError("margin_policy_supersedes_required")
            _require_hash(self.supersedes_hash, "margin_policy.supersedes_hash")

    def policy_hash(self) -> str:
        return sha256_prefixed(
            {
                "policy_id": self.policy_id,
                "version": self.version,
                "exchange_mic": self.exchange_mic,
                "currency": self.currency,
                "initial_per_contract": _decimal_text(self.initial_per_contract),
                "maintenance_per_contract": _decimal_text(
                    self.maintenance_per_contract
                ),
                "variation_frequency": self.variation_frequency,
                "collateral": [
                    {
                        "asset_id": item.asset_id,
                        "kind": item.kind.value,
                        "currency": item.currency,
                        "haircut": _decimal_text(item.haircut),
                        "concentration_limit": _decimal_text(item.concentration_limit),
                        "source_hash": item.source_hash,
                    }
                    for item in self.collateral
                ],
                "collateral_waterfall": list(self.collateral_waterfall),
                "collateral_yield_rate": _decimal_text(self.collateral_yield_rate),
                "spread_offset_rate": _decimal_text(self.spread_offset_rate),
                "additional_funding_allowed": self.additional_funding_allowed,
                "metadata": self.metadata.as_dict(),
                "supersedes_hash": self.supersedes_hash,
            },
            label="futures_margin_policy",
        )


@dataclass(frozen=True, slots=True)
class CollateralHolding:
    asset_id: str
    market_value: Decimal

    def __post_init__(self) -> None:
        _require_id(self.asset_id, "collateral_holding.asset_id")
        object.__setattr__(
            self,
            "market_value",
            _decimal(
                self.market_value,
                "collateral_holding.market_value",
                nonnegative=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class MarginWaterfallResult:
    gross_initial_requirement: Decimal
    spread_offset: Decimal
    net_initial_requirement: Decimal
    maintenance_requirement: Decimal
    eligible_collateral_value: Decimal
    variation_margin: Decimal
    collateral_income: Decimal
    margin_call: Decimal
    additional_funding: Decimal
    default_amount: Decimal
    consumed_assets: tuple[tuple[str, Decimal], ...]
    policy_hash: str

    def result_hash(self) -> str:
        return sha256_prefixed(
            {
                "gross_initial_requirement": _decimal_text(
                    self.gross_initial_requirement
                ),
                "spread_offset": _decimal_text(self.spread_offset),
                "net_initial_requirement": _decimal_text(self.net_initial_requirement),
                "maintenance_requirement": _decimal_text(self.maintenance_requirement),
                "eligible_collateral_value": _decimal_text(
                    self.eligible_collateral_value
                ),
                "variation_margin": _decimal_text(self.variation_margin),
                "collateral_income": _decimal_text(self.collateral_income),
                "margin_call": _decimal_text(self.margin_call),
                "additional_funding": _decimal_text(self.additional_funding),
                "default_amount": _decimal_text(self.default_amount),
                "consumed_assets": [
                    {"asset_id": key, "amount": _decimal_text(value)}
                    for key, value in self.consumed_assets
                ],
                "policy_hash": self.policy_hash,
            },
            label="futures_margin_waterfall_result",
        )


def evaluate_margin_waterfall(
    policy: FuturesMarginPolicyVersion,
    *,
    outright_contracts: Decimal,
    spread_contract_pairs: Decimal,
    variation_margin: Decimal,
    collateral_holdings: Sequence[CollateralHolding],
    elapsed_days: Decimal,
) -> MarginWaterfallResult:
    """Apply versioned exchange margin, offsets, collateral, and funding."""

    outrights = _decimal(
        outright_contracts, "margin.outright_contracts", nonnegative=True
    )
    spread_pairs = _decimal(
        spread_contract_pairs, "margin.spread_contract_pairs", nonnegative=True
    )
    variation = _decimal(variation_margin, "margin.variation_margin")
    days = _decimal(elapsed_days, "margin.elapsed_days", nonnegative=True)
    gross = (outrights + spread_pairs * Decimal("2")) * (policy.initial_per_contract)
    offset = (
        spread_pairs
        * Decimal("2")
        * policy.initial_per_contract
        * policy.spread_offset_rate
    )
    net = gross - offset
    maintenance = (outrights + spread_pairs * Decimal("2")) * (
        policy.maintenance_per_contract
    )
    holdings = {item.asset_id: item.market_value for item in collateral_holdings}
    eligibility = {item.asset_id: item for item in policy.collateral}
    if set(holdings) - set(eligibility):
        raise FuturesDeliveryError("ineligible_collateral_asset")
    eligible_values: dict[str, Decimal] = {}
    for asset_id, market_value in holdings.items():
        rule = eligibility[asset_id]
        eligible_values[asset_id] = market_value * (_ONE - rule.haircut)
    total_eligible = sum(eligible_values.values(), _ZERO)
    for asset_id, value in eligible_values.items():
        rule = eligibility[asset_id]
        if total_eligible > 0 and value / total_eligible > rule.concentration_limit:
            raise FuturesDeliveryError(
                f"collateral_concentration_limit_exceeded:{asset_id}"
            )
    collateral_income = (
        total_eligible * policy.collateral_yield_rate * days / Decimal("365")
    )
    available = total_eligible + collateral_income + variation
    call = max(net - available, _ZERO)
    funding = call if call > 0 and policy.additional_funding_allowed else _ZERO
    default = call - funding
    remaining_need = max(net - variation - collateral_income, _ZERO)
    consumed: list[tuple[str, Decimal]] = []
    for asset_id in policy.collateral_waterfall:
        amount = min(eligible_values.get(asset_id, _ZERO), remaining_need)
        if amount > 0:
            consumed.append((asset_id, amount))
            remaining_need -= amount
    return MarginWaterfallResult(
        gross_initial_requirement=gross,
        spread_offset=offset,
        net_initial_requirement=net,
        maintenance_requirement=maintenance,
        eligible_collateral_value=total_eligible,
        variation_margin=variation,
        collateral_income=collateral_income,
        margin_call=call,
        additional_funding=funding,
        default_amount=default,
        consumed_assets=tuple(consumed),
        policy_hash=policy.policy_hash(),
    )
