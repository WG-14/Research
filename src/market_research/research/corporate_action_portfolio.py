"""Causal portfolio-event plans derived from immutable corporate actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .corporate_action_contract import (
    CorporateActionContractError,
    CorporateActionEvent,
    CorporateActionSet,
)
from .hashing import sha256_prefixed


PORTFOLIO_EVENT_PLAN_SCHEMA_VERSION = 1
PORTFOLIO_EVENT_TIME_POLICY = (
    "latest_version_known_and_effective_at_decision_boundary"
)
SUPPORTED_PORTFOLIO_EVENT_TYPES = frozenset(
    {
        "cash_dividend",
        "stock_dividend",
        "split",
        "reverse_split",
        "delisting",
        "trading_halt",
        "trading_resume",
        "ticker_change",
        "etf_distribution",
        "etf_merger",
        "etf_liquidation",
    }
)
TERMINAL_PORTFOLIO_EVENT_TYPES = frozenset(
    {"delisting", "etf_merger", "etf_liquidation"}
)
QUANTITY_ADJUSTMENT_EVENT_TYPES = frozenset(
    {"split", "reverse_split", "stock_dividend"}
)
CASH_INCOME_EVENT_TYPES = frozenset({"cash_dividend", "etf_distribution"})
TRADABILITY_EVENT_TYPES = frozenset({"trading_halt", "trading_resume"})


class CorporateActionPortfolioError(CorporateActionContractError):
    """Portfolio-event evidence is invalid or cannot be applied causally."""


@dataclass(frozen=True, slots=True)
class CorporateActionPortfolioEvent:
    event_id: str
    event_version_id: str
    version: int
    instrument_id: str
    event_type: str
    effective_at: str
    published_at: str
    observed_at: str
    source_content_hash: str
    event_contract_hash: str
    ratio: str | None = None
    cash_amount: str | None = None
    cash_currency: str | None = None
    replacement_symbol: str | None = None
    replacement_instrument_id: str | None = None
    tradability: str | None = None

    @classmethod
    def from_event(cls, event: CorporateActionEvent) -> "CorporateActionPortfolioEvent":
        payload = event.as_dict()
        return cls.from_payload(
            {"event": payload, "event_contract_hash": event.contract_hash()}
        )

    @classmethod
    def from_payload(
        cls, value: Mapping[str, object]
    ) -> "CorporateActionPortfolioEvent":
        event = value.get("event")
        if not isinstance(event, Mapping):
            raise CorporateActionPortfolioError(
                "corporate_action_portfolio_event_payload_required"
            )
        payload = dict(event)
        event_contract_hash = value.get("event_contract_hash")
        calculated = sha256_prefixed(payload, label="corporate_action_event")
        if event_contract_hash != calculated:
            raise CorporateActionPortfolioError(
                "corporate_action_portfolio_event_hash_mismatch"
            )
        try:
            version = int(payload["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CorporateActionPortfolioError(
                "corporate_action_portfolio_event_version_invalid"
            ) from exc
        event_type = _required_text(payload.get("event_type"), "event_type")
        effective_at = _required_timestamp(payload.get("effective_at"), "effective_at")
        observed_at = _required_timestamp(payload.get("observed_at"), "observed_at")
        published_at = _required_timestamp(payload.get("published_at"), "published_at")
        ratio = _optional_decimal_text(payload.get("ratio"), "ratio", positive=True)
        cash_amount = _optional_decimal_text(
            payload.get("cash_amount"), "cash_amount", nonnegative=True
        )
        return cls(
            event_id=_required_text(payload.get("event_id"), "event_id"),
            event_version_id=_required_text(
                payload.get("event_version_id"), "event_version_id"
            ),
            version=version,
            instrument_id=_required_text(
                payload.get("instrument_id"), "instrument_id"
            ),
            event_type=event_type,
            effective_at=effective_at,
            published_at=published_at,
            observed_at=observed_at,
            source_content_hash=_required_text(
                payload.get("source_content_hash"), "source_content_hash"
            ),
            event_contract_hash=str(event_contract_hash),
            ratio=ratio,
            cash_amount=cash_amount,
            cash_currency=_optional_text(payload.get("cash_currency")),
            replacement_symbol=_optional_text(payload.get("replacement_symbol")),
            replacement_instrument_id=_optional_text(
                payload.get("replacement_instrument_id")
            ),
            tradability=_optional_text(payload.get("tradability")),
        )

    @property
    def effective_ts_ms(self) -> int:
        return _timestamp_ms(self.effective_at)

    @property
    def observed_ts_ms(self) -> int:
        return _timestamp_ms(self.observed_at)

    @property
    def ratio_value(self) -> float | None:
        return float(Decimal(self.ratio)) if self.ratio is not None else None

    @property
    def cash_amount_value(self) -> float | None:
        return (
            float(Decimal(self.cash_amount))
            if self.cash_amount is not None
            else None
        )

    def as_dict(self) -> dict[str, object]:
        event = {
            "schema_version": 1,
            "event_id": self.event_id,
            "event_version_id": self.event_version_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type,
            "effective_at": self.effective_at,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "source_content_hash": self.source_content_hash,
            "ratio": self.ratio,
            "cash_amount": self.cash_amount,
            "cash_currency": self.cash_currency,
            "replacement_symbol": self.replacement_symbol,
            "replacement_instrument_id": self.replacement_instrument_id,
            "tradability": self.tradability,
        }
        return {"event": event, "event_contract_hash": self.event_contract_hash}

    def behavior_terms_hash(self) -> str:
        return sha256_prefixed(
            {
                "event_contract_hash": self.event_contract_hash,
                "event_type": self.event_type,
                "ratio": self.ratio,
                "cash_amount": self.cash_amount,
                "cash_currency": self.cash_currency,
                "replacement_symbol": self.replacement_symbol,
                "replacement_instrument_id": self.replacement_instrument_id,
                "tradability": self.tradability,
            },
            label="corporate_action_portfolio_behavior_terms",
        )


@dataclass(frozen=True, slots=True)
class CorporateActionPortfolioPlan:
    action_set_id: str
    action_set_hash: str
    instrument_id: str
    trading_currency: str
    quantity_step: str
    manifest_known_at: str
    events: tuple[CorporateActionPortfolioEvent, ...]
    plan_hash: str

    def material(self) -> dict[str, object]:
        return {
            "schema_version": PORTFOLIO_EVENT_PLAN_SCHEMA_VERSION,
            "action_set_id": self.action_set_id,
            "action_set_hash": self.action_set_hash,
            "instrument_id": self.instrument_id,
            "trading_currency": self.trading_currency,
            "quantity_step": self.quantity_step,
            "manifest_known_at": self.manifest_known_at,
            "event_time_policy": PORTFOLIO_EVENT_TIME_POLICY,
            "same_timestamp_precedence": "corporate_action_before_execution_fill",
            "same_timestamp_action_policy": (
                "commutative_event_sets_only_else_fail_closed"
            ),
            "late_observation_policy": "fail_closed_no_retroactive_application",
            "post_application_correction_policy": "fail_closed",
            "raw_execution_price_policy": "never_adjust_execution_candles",
            "cash_amount_semantics": (
                "manifest_declared_cash_per_position_unit_no_additional_tax_model"
            ),
            "fractional_entitlement_policy": (
                "require_quantity_step_alignment_else_fail_closed"
            ),
            "identity_transition_policy": (
                "stable_internal_instrument_with_manifest_mapping_only"
            ),
            "events": [event.as_dict() for event in self.events],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.material(), "plan_hash": self.plan_hash}


def build_corporate_action_portfolio_plan(
    *,
    action_set: CorporateActionSet,
    manifest_known_at: str,
    trading_currency: str,
    quantity_step: str,
) -> CorporateActionPortfolioPlan:
    _required_timestamp(manifest_known_at, "manifest_known_at")
    canonical_quantity_step = _optional_decimal_text(
        quantity_step, "quantity_step", positive=True
    )
    if canonical_quantity_step is None:
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_quantity_step_required"
        )
    events = tuple(
        CorporateActionPortfolioEvent.from_event(event)
        for event in action_set.causally_available(as_of=manifest_known_at)
    )
    provisional = CorporateActionPortfolioPlan(
        action_set_id=action_set.action_set_id,
        action_set_hash=action_set.contract_hash(),
        instrument_id=action_set.instrument_id,
        trading_currency=trading_currency,
        quantity_step=canonical_quantity_step,
        manifest_known_at=manifest_known_at,
        events=events,
        plan_hash="",
    )
    return CorporateActionPortfolioPlan(
        action_set_id=provisional.action_set_id,
        action_set_hash=provisional.action_set_hash,
        instrument_id=provisional.instrument_id,
        trading_currency=provisional.trading_currency,
        quantity_step=provisional.quantity_step,
        manifest_known_at=provisional.manifest_known_at,
        events=provisional.events,
        plan_hash=sha256_prefixed(
            provisional.material(), label="corporate_action_portfolio_plan"
        ),
    )


def parse_corporate_action_portfolio_plan(
    value: Mapping[str, object],
) -> CorporateActionPortfolioPlan:
    events_value = value.get("events")
    if not isinstance(events_value, (list, tuple)):
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_plan_events_required"
        )
    events = tuple(
        CorporateActionPortfolioEvent.from_payload(item)
        for item in events_value
        if isinstance(item, Mapping)
    )
    if len(events) != len(events_value):
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_plan_event_invalid"
        )
    quantity_step = _optional_decimal_text(
        value.get("quantity_step"), "quantity_step", positive=True
    )
    if quantity_step is None:
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_quantity_step_required"
        )
    plan = CorporateActionPortfolioPlan(
        action_set_id=_required_text(value.get("action_set_id"), "action_set_id"),
        action_set_hash=_required_text(
            value.get("action_set_hash"), "action_set_hash"
        ),
        instrument_id=_required_text(value.get("instrument_id"), "instrument_id"),
        trading_currency=_required_text(
            value.get("trading_currency"), "trading_currency"
        ),
        quantity_step=quantity_step,
        manifest_known_at=_required_timestamp(
            value.get("manifest_known_at"), "manifest_known_at"
        ),
        events=events,
        plan_hash=_required_text(value.get("plan_hash"), "plan_hash"),
    )
    if value.get("schema_version") != PORTFOLIO_EVENT_PLAN_SCHEMA_VERSION:
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_plan_schema_unsupported"
        )
    if any(event.instrument_id != plan.instrument_id for event in plan.events):
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_plan_instrument_mismatch"
        )
    if any(
        event.cash_currency is not None
        and event.cash_currency != plan.trading_currency
        for event in plan.events
    ):
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_plan_cash_currency_mismatch"
        )
    expected_constants = {
        "event_time_policy": PORTFOLIO_EVENT_TIME_POLICY,
        "same_timestamp_precedence": "corporate_action_before_execution_fill",
        "same_timestamp_action_policy": (
            "commutative_event_sets_only_else_fail_closed"
        ),
        "late_observation_policy": "fail_closed_no_retroactive_application",
        "post_application_correction_policy": "fail_closed",
        "raw_execution_price_policy": "never_adjust_execution_candles",
        "cash_amount_semantics": (
            "manifest_declared_cash_per_position_unit_no_additional_tax_model"
        ),
        "fractional_entitlement_policy": (
            "require_quantity_step_alignment_else_fail_closed"
        ),
        "identity_transition_policy": (
            "stable_internal_instrument_with_manifest_mapping_only"
        ),
    }
    if any(value.get(key) != expected for key, expected in expected_constants.items()):
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_plan_policy_mismatch"
        )
    calculated = sha256_prefixed(
        plan.material(), label="corporate_action_portfolio_plan"
    )
    if plan.plan_hash != calculated:
        raise CorporateActionPortfolioError(
            "corporate_action_portfolio_plan_hash_mismatch"
        )
    return plan


def latest_causally_applicable_events(
    events: tuple[CorporateActionPortfolioEvent, ...], *, boundary_ms: int
) -> tuple[CorporateActionPortfolioEvent, ...]:
    latest_known: dict[str, CorporateActionPortfolioEvent] = {}
    for event in events:
        if event.observed_ts_ms > boundary_ms:
            continue
        current = latest_known.get(event.event_id)
        if current is None or event.version > current.version:
            latest_known[event.event_id] = event
    return tuple(
        sorted(
            (
                event
                for event in latest_known.values()
                if event.effective_ts_ms <= boundary_ms
            ),
            key=lambda event: (
                event.effective_ts_ms,
                event.event_id,
                event.version,
            ),
        )
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorporateActionPortfolioError(
            f"corporate_action_portfolio_{field}_required"
        )
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_timestamp(value: object, field: str) -> str:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorporateActionPortfolioError(
            f"corporate_action_portfolio_{field}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorporateActionPortfolioError(
            f"corporate_action_portfolio_{field}_timezone_required"
        )
    if parsed.microsecond % 1000 != 0:
        raise CorporateActionPortfolioError(
            f"corporate_action_portfolio_{field}_millisecond_alignment_required"
        )
    return text


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _optional_decimal_text(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> str | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CorporateActionPortfolioError(
            f"corporate_action_portfolio_{field}_invalid"
        ) from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (
        nonnegative and parsed < 0
    ):
        raise CorporateActionPortfolioError(
            f"corporate_action_portfolio_{field}_invalid"
        )
    return format(parsed.normalize(), "f") if parsed != 0 else "0"
