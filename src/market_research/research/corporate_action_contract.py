"""Causal, immutable corporate-action and product-event contracts.

Events are externally prepared research inputs.  This module validates their
identity, event time, publication time, observation time, and adjustment
policy; it never discovers, retries, or backfills events from a network source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .hashing import sha256_prefixed
from .instrument_contract import (
    InstrumentContractError,
    decimal_text,
    decimal_value,
    require_hash,
)


CORPORATE_ACTION_SCHEMA_VERSION = 1
CORPORATE_ACTION_ACCOUNTING_SCHEMA_VERSION = 1
SUPPORTED_CORPORATE_ACTION_SCHEMA_VERSIONS = frozenset({1, 2})
CORPORATE_ACTION_EMBEDDED_EVENT_MATERIAL_HASH_LABEL = (
    "corporate_action_embedded_event_material"
)
_EVENT_ID = re.compile(r"^ca_[a-z0-9][a-z0-9_-]{7,63}$")
_VERSION_ID = re.compile(r"^cav_[a-z0-9][a-z0-9_-]{7,63}$")
_INSTRUMENT_ID = re.compile(r"^inst_[a-z0-9][a-z0-9_-]{7,63}$")
_POLICY_ID = re.compile(r"^cap_[a-z0-9][a-z0-9_-]{7,63}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_EVENT_TYPES = frozenset(
    {
        "cash_dividend",
        "stock_dividend",
        "split",
        "reverse_split",
        "capital_reduction",
        "delisting",
        "trading_halt",
        "trading_resume",
        "ticker_change",
        "etf_distribution",
        "etf_merger",
        "etf_liquidation",
    }
)
_V2_EVENT_TYPES = _EVENT_TYPES | frozenset(
    {
        "special_dividend",
        "rights_issue",
        "ex_rights",
        "spin_off",
        "merger",
    }
)
_V2_SETTLEMENT_POLICIES = frozenset(
    {
        "quantity_adjustment",
        "cash_distribution",
        "capital_reduction",
        "rights_cash_entitlement",
        "rights_subscription",
        "ex_rights_marker",
        "cash_settled_spin_off",
        "stock_merger",
        "mixed_merger",
        "cash_exit",
        "tradability_transition",
        "identity_metadata",
    }
)


class CorporateActionContractError(ValueError):
    """Corporate-action evidence is incomplete or contradictory."""


def corporate_action_embedded_event_material_hash(
    value: Mapping[str, object],
) -> str:
    """Bind canonical event material without claiming external-source proof.

    This detects mutation of the embedded event and its declared external
    ``source_content_hash``.  It deliberately does not claim that provider
    bytes were fetched or authenticated; that needs a separate source-artifact
    contract which schema-v2 does not currently possess.
    """

    material = dict(value)
    material.pop("embedded_event_material_hash", None)
    return sha256_prefixed(
        material,
        label=CORPORATE_ACTION_EMBEDDED_EVENT_MATERIAL_HASH_LABEL,
    )


@dataclass(frozen=True, slots=True)
class CorporateActionAccountingTerms:
    """Reviewed, replayable economics for schema-v2 events.

    Every field is explicit, including zero tax and no cash-in-lieu.  This is
    intentionally not inferred from an event name: externally prepared terms
    are the accounting authority and their canonical representation is part of
    the event contract hash.
    """

    schema_version: int
    settlement_policy: str
    same_timestamp_sequence: int
    entitlement_basis: str
    position_effect: str
    position_ratio: Decimal
    cash_per_pre_event_unit: Decimal
    cash_currency: str | None
    tax_policy: str
    tax_rate: Decimal
    basis_policy: str
    cash_basis_fraction: Decimal
    fractional_policy: str
    cash_in_lieu_price: Decimal | None
    cash_in_lieu_tax_rate: Decimal
    terminal: bool
    continuation_price_policy: str

    def __post_init__(self) -> None:
        if self.schema_version != CORPORATE_ACTION_ACCOUNTING_SCHEMA_VERSION:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_schema_unsupported"
            )
        if self.settlement_policy not in _V2_SETTLEMENT_POLICIES:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_settlement_policy_unknown"
            )
        if (
            isinstance(self.same_timestamp_sequence, bool)
            or self.same_timestamp_sequence < 1
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_same_timestamp_sequence_invalid"
            )
        if self.entitlement_basis != "position_immediately_before_event":
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_entitlement_basis_unsupported"
            )
        if self.position_effect not in {"unchanged", "scale", "replace", "close"}:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_position_effect_unknown"
            )
        if (
            not self.position_ratio.is_finite()
            or self.position_ratio < 0
            or (
                self.position_effect in {"scale", "replace"}
                and self.position_ratio <= 0
            )
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_position_ratio_invalid"
            )
        expected_ratio = {
            "unchanged": Decimal("1"),
            "close": Decimal("0"),
        }.get(self.position_effect)
        if expected_ratio is not None and self.position_ratio != expected_ratio:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_position_ratio_mismatch"
            )
        if not self.cash_per_pre_event_unit.is_finite():
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_non_finite"
            )
        if self.cash_currency is not None and not _CURRENCY.fullmatch(
            self.cash_currency
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_currency_invalid"
            )
        if self.tax_policy not in {
            "none",
            "gross_cash_rate",
            "gain_over_allocated_basis_rate",
        }:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_tax_policy_unknown"
            )
        if not self.tax_rate.is_finite() or not Decimal(
            "0"
        ) <= self.tax_rate <= Decimal("1"):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_tax_rate_invalid"
            )
        if self.tax_policy == "none" and self.tax_rate != 0:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_tax_rate_not_applicable"
            )
        if self.cash_per_pre_event_unit < 0 and self.tax_policy != "none":
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_outflow_cannot_be_taxed"
            )
        if self.cash_per_pre_event_unit == 0 and self.tax_policy != "none":
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_primary_tax_not_applicable"
            )
        if self.basis_policy not in {
            "preserve",
            "allocate_cash_fraction",
            "add_cash_outflow",
            "close",
        }:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_basis_policy_unknown"
            )
        if not self.cash_basis_fraction.is_finite() or not Decimal(
            "0"
        ) <= self.cash_basis_fraction <= Decimal("1"):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_basis_fraction_invalid"
            )
        if self.basis_policy in {"preserve", "add_cash_outflow"} and (
            self.cash_basis_fraction != 0
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_basis_not_applicable"
            )
        if self.basis_policy == "close" and self.cash_basis_fraction != 1:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_close_must_release_all_basis"
            )
        if self.fractional_policy not in {
            "retain_exact",
            "reject_non_step",
            "round_down_cash_in_lieu",
        }:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_fractional_policy_unknown"
            )
        if self.position_effect not in {"scale", "replace"} and (
            self.fractional_policy != "retain_exact"
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_fractional_policy_not_applicable"
            )
        if not self.cash_in_lieu_tax_rate.is_finite() or not Decimal(
            "0"
        ) <= self.cash_in_lieu_tax_rate <= Decimal("1"):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_in_lieu_tax_rate_invalid"
            )
        if self.fractional_policy == "round_down_cash_in_lieu":
            if (
                self.cash_in_lieu_price is None
                or not self.cash_in_lieu_price.is_finite()
                or self.cash_in_lieu_price <= 0
                or self.cash_currency is None
            ):
                raise CorporateActionContractError(
                    "corporate_action.accounting_terms_cash_in_lieu_terms_required"
                )
        elif self.cash_in_lieu_price is not None or self.cash_in_lieu_tax_rate != 0:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_in_lieu_not_applicable"
            )
        if not isinstance(self.terminal, bool):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_terminal_must_be_boolean"
            )
        if self.terminal != (self.position_effect == "close"):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_terminal_position_mismatch"
            )
        if self.continuation_price_policy not in {
            "not_applicable",
            "prepared_raw_series_switches_to_replacement_at_effective",
        }:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_continuation_policy_unknown"
            )
        if self.position_effect == "replace":
            if self.continuation_price_policy != (
                "prepared_raw_series_switches_to_replacement_at_effective"
            ):
                raise CorporateActionContractError(
                    "corporate_action.accounting_terms_replacement_price_policy_required"
                )
        elif self.continuation_price_policy != "not_applicable":
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_continuation_policy_not_applicable"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "settlement_policy": self.settlement_policy,
            "same_timestamp_sequence": self.same_timestamp_sequence,
            "entitlement_basis": self.entitlement_basis,
            "position_effect": self.position_effect,
            "position_ratio": decimal_text(self.position_ratio),
            "cash_per_pre_event_unit": decimal_text(self.cash_per_pre_event_unit),
            "cash_currency": self.cash_currency,
            "tax_policy": self.tax_policy,
            "tax_rate": decimal_text(self.tax_rate),
            "basis_policy": self.basis_policy,
            "cash_basis_fraction": decimal_text(self.cash_basis_fraction),
            "fractional_policy": self.fractional_policy,
            "cash_in_lieu_price": (
                decimal_text(self.cash_in_lieu_price)
                if self.cash_in_lieu_price is not None
                else None
            ),
            "cash_in_lieu_tax_rate": decimal_text(self.cash_in_lieu_tax_rate),
            "terminal": self.terminal,
            "continuation_price_policy": self.continuation_price_policy,
        }


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    schema_version: int
    event_id: str
    event_version_id: str
    version: int
    instrument_id: str
    event_type: str
    effective_at: str
    published_at: str
    observed_at: str
    source_content_hash: str
    embedded_event_material_hash: str | None = None
    ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    cash_currency: str | None = None
    replacement_symbol: str | None = None
    replacement_instrument_id: str | None = None
    tradability: str | None = None
    accounting_terms: CorporateActionAccountingTerms | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CORPORATE_ACTION_SCHEMA_VERSIONS:
            raise CorporateActionContractError("corporate_action_schema_unsupported")
        if not _EVENT_ID.fullmatch(self.event_id):
            raise CorporateActionContractError("corporate_action.event_id_invalid")
        if not _VERSION_ID.fullmatch(self.event_version_id):
            raise CorporateActionContractError(
                "corporate_action.event_version_id_invalid"
            )
        if isinstance(self.version, bool) or self.version < 1:
            raise CorporateActionContractError("corporate_action.version_invalid")
        if not _INSTRUMENT_ID.fullmatch(self.instrument_id):
            raise CorporateActionContractError("corporate_action.instrument_id_invalid")
        supported_types = _EVENT_TYPES if self.schema_version == 1 else _V2_EVENT_TYPES
        if self.event_type not in supported_types:
            raise CorporateActionContractError("corporate_action.event_type_unknown")
        effective = _timestamp(self.effective_at, "corporate_action.effective_at")
        published = _timestamp(self.published_at, "corporate_action.published_at")
        observed = _timestamp(self.observed_at, "corporate_action.observed_at")
        if any(
            value.microsecond % 1000 != 0 for value in (effective, published, observed)
        ):
            raise CorporateActionContractError(
                "corporate_action_timestamp_millisecond_alignment_required"
            )
        if observed < published:
            raise CorporateActionContractError(
                "corporate_action_observed_before_publication"
            )
        try:
            require_hash(
                self.source_content_hash, "corporate_action.source_content_hash"
            )
        except InstrumentContractError as exc:
            raise CorporateActionContractError(str(exc)) from exc
        if self.schema_version == 2:
            self._validate_v2_terms()
            if self.embedded_event_material_hash is None or not _HASH.fullmatch(
                self.embedded_event_material_hash
            ):
                raise CorporateActionContractError(
                    "corporate_action.embedded_event_material_hash_required"
                )
            expected_material_hash = corporate_action_embedded_event_material_hash(
                self.as_dict()
            )
            if self.embedded_event_material_hash != expected_material_hash:
                raise CorporateActionContractError(
                    "corporate_action.embedded_event_material_hash_content_mismatch"
                )
            return
        if self.embedded_event_material_hash is not None:
            raise CorporateActionContractError(
                "corporate_action.embedded_event_material_hash_not_applicable"
            )
        if self.accounting_terms is not None:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_not_applicable_to_schema_v1"
            )
        if self.event_type in {"split", "reverse_split", "stock_dividend"}:
            if self.ratio is None or not self.ratio.is_finite() or self.ratio <= 0:
                raise CorporateActionContractError("corporate_action.ratio_required")
        elif self.ratio is not None:
            raise CorporateActionContractError("corporate_action.ratio_not_applicable")
        if self.event_type in {"cash_dividend", "etf_distribution"}:
            if (
                self.cash_amount is None
                or not self.cash_amount.is_finite()
                or self.cash_amount < 0
                or self.cash_currency is None
                or not _CURRENCY.fullmatch(self.cash_currency)
            ):
                raise CorporateActionContractError(
                    "corporate_action.cash_amount_and_currency_required"
                )
        elif self.event_type in {"delisting", "etf_merger", "etf_liquidation"}:
            if (self.cash_amount is None) != (self.cash_currency is None):
                raise CorporateActionContractError(
                    "corporate_action.terminal_cash_terms_must_be_complete"
                )
            if self.cash_amount is not None and (
                not self.cash_amount.is_finite()
                or self.cash_amount < 0
                or self.cash_currency is None
                or not _CURRENCY.fullmatch(self.cash_currency)
            ):
                raise CorporateActionContractError(
                    "corporate_action.terminal_cash_terms_invalid"
                )
        elif self.cash_amount is not None or self.cash_currency is not None:
            raise CorporateActionContractError(
                "corporate_action.cash_terms_not_applicable"
            )
        if self.event_type == "ticker_change":
            if self.replacement_symbol is None or not self.replacement_symbol.strip():
                raise CorporateActionContractError(
                    "corporate_action.replacement_symbol_required"
                )
        elif self.replacement_symbol is not None:
            raise CorporateActionContractError(
                "corporate_action.replacement_symbol_not_applicable"
            )
        if self.replacement_instrument_id is not None and not _INSTRUMENT_ID.fullmatch(
            self.replacement_instrument_id
        ):
            raise CorporateActionContractError(
                "corporate_action.replacement_instrument_id_invalid"
            )
        expected_tradability = {
            "trading_halt": "halted",
            "trading_resume": "tradable",
            "delisting": "delisted",
            "etf_liquidation": "delisted",
        }.get(self.event_type)
        if (
            expected_tradability is not None
            and self.tradability != expected_tradability
        ):
            raise CorporateActionContractError(
                "corporate_action.tradability_transition_invalid"
            )
        if expected_tradability is None and self.tradability is not None:
            raise CorporateActionContractError(
                "corporate_action.tradability_not_applicable"
            )
        # Event time may precede or follow publication.  Keeping both is the
        # point; only observation time controls causal availability.
        del effective

    def _validate_v2_terms(self) -> None:
        terms = self.accounting_terms
        if terms is None:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_required_for_schema_v2"
            )
        ratio_applies = terms.position_effect in {"scale", "replace"}
        if ratio_applies:
            if self.ratio != terms.position_ratio:
                raise CorporateActionContractError(
                    "corporate_action.accounting_terms_ratio_binding_mismatch"
                )
        elif self.ratio is not None:
            raise CorporateActionContractError("corporate_action.ratio_not_applicable")
        cash_terms_required = terms.settlement_policy in {
            "cash_distribution",
            "capital_reduction",
            "rights_cash_entitlement",
            "rights_subscription",
            "cash_settled_spin_off",
            "mixed_merger",
            "cash_exit",
        }
        if cash_terms_required:
            if (
                self.cash_amount != terms.cash_per_pre_event_unit
                or self.cash_currency != terms.cash_currency
                or self.cash_currency is None
            ):
                raise CorporateActionContractError(
                    "corporate_action.accounting_terms_cash_binding_mismatch"
                )
        elif self.cash_amount is not None:
            raise CorporateActionContractError(
                "corporate_action.cash_terms_not_applicable"
            )
        elif self.cash_currency != terms.cash_currency:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_currency_binding_mismatch"
            )
        if terms.cash_currency is None and (
            terms.cash_per_pre_event_unit != 0 or terms.cash_in_lieu_price is not None
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_currency_required"
            )
        if terms.cash_currency is not None and (
            terms.cash_per_pre_event_unit == 0
            and terms.cash_in_lieu_price is None
            and not cash_terms_required
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_currency_not_applicable"
            )
        if terms.position_effect == "replace":
            if (
                self.replacement_instrument_id is None
                or self.replacement_instrument_id == self.instrument_id
            ):
                raise CorporateActionContractError(
                    "corporate_action.replacement_instrument_id_required"
                )
        elif terms.settlement_policy == "cash_settled_spin_off":
            if (
                self.replacement_instrument_id is None
                or self.replacement_instrument_id == self.instrument_id
            ):
                raise CorporateActionContractError(
                    "corporate_action.spin_off_child_instrument_id_required"
                )
        elif self.replacement_instrument_id is not None and self.event_type != (
            "ticker_change"
        ):
            raise CorporateActionContractError(
                "corporate_action.replacement_instrument_id_not_applicable"
            )
        if self.replacement_instrument_id is not None and not _INSTRUMENT_ID.fullmatch(
            self.replacement_instrument_id
        ):
            raise CorporateActionContractError(
                "corporate_action.replacement_instrument_id_invalid"
            )
        expected_policy = {
            "cash_dividend": {"cash_distribution"},
            "special_dividend": {"cash_distribution"},
            "etf_distribution": {"cash_distribution"},
            "split": {"quantity_adjustment"},
            "reverse_split": {"quantity_adjustment"},
            "stock_dividend": {"quantity_adjustment"},
            "capital_reduction": {"capital_reduction"},
            "rights_issue": {"rights_cash_entitlement", "rights_subscription"},
            "ex_rights": {"ex_rights_marker"},
            "spin_off": {"cash_settled_spin_off"},
            "merger": {"cash_exit", "stock_merger", "mixed_merger"},
            "etf_merger": {"cash_exit", "stock_merger", "mixed_merger"},
            "delisting": {"cash_exit"},
            "etf_liquidation": {"cash_exit"},
            "trading_halt": {"tradability_transition"},
            "trading_resume": {"tradability_transition"},
            "ticker_change": {"identity_metadata"},
        }.get(self.event_type)
        if expected_policy is None or terms.settlement_policy not in expected_policy:
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_event_type_mismatch"
            )
        self._validate_v2_settlement_shape(terms)
        expected_tradability = {
            "trading_halt": "halted",
            "trading_resume": "tradable",
            "delisting": "delisted",
            "etf_liquidation": "delisted",
        }.get(self.event_type)
        if expected_tradability is not None:
            if self.tradability != expected_tradability:
                raise CorporateActionContractError(
                    "corporate_action.tradability_transition_invalid"
                )
        elif self.tradability is not None:
            raise CorporateActionContractError(
                "corporate_action.tradability_not_applicable"
            )

    def _validate_v2_settlement_shape(
        self, terms: CorporateActionAccountingTerms
    ) -> None:
        policy = terms.settlement_policy
        no_economic_change = {
            "ex_rights_marker",
            "tradability_transition",
            "identity_metadata",
        }
        if policy in no_economic_change and (
            terms.position_effect != "unchanged"
            or terms.cash_per_pre_event_unit != 0
            or terms.basis_policy != "preserve"
            or terms.terminal
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_noop_shape_invalid"
            )
        if policy == "quantity_adjustment" and (
            terms.position_effect != "scale"
            or terms.position_ratio == 1
            or terms.cash_per_pre_event_unit != 0
            or terms.basis_policy != "preserve"
            or terms.terminal
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_quantity_shape_invalid"
            )
        if policy == "cash_distribution" and (
            terms.position_effect != "unchanged"
            or terms.cash_per_pre_event_unit <= 0
            or terms.basis_policy != "preserve"
            or terms.terminal
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_distribution_shape_invalid"
            )
        if policy == "capital_reduction" and (
            terms.position_effect != "scale"
            or not Decimal("0") < terms.position_ratio < Decimal("1")
            or terms.cash_per_pre_event_unit < 0
            or terms.terminal
            or terms.basis_policy not in {"preserve", "allocate_cash_fraction"}
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_capital_reduction_shape_invalid"
            )
        if policy == "rights_cash_entitlement" and (
            terms.position_effect != "unchanged"
            or terms.cash_per_pre_event_unit <= 0
            or terms.basis_policy != "preserve"
            or terms.terminal
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_rights_cash_shape_invalid"
            )
        if policy == "rights_subscription" and (
            terms.position_effect != "scale"
            or terms.position_ratio <= 1
            or terms.cash_per_pre_event_unit >= 0
            or terms.basis_policy != "add_cash_outflow"
            or terms.terminal
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_rights_subscription_shape_invalid"
            )
        if policy == "cash_settled_spin_off" and (
            terms.position_effect != "unchanged"
            or terms.cash_per_pre_event_unit <= 0
            or terms.basis_policy != "allocate_cash_fraction"
            or terms.cash_basis_fraction <= 0
            or terms.terminal
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_spin_off_shape_invalid"
            )
        if policy in {"stock_merger", "mixed_merger"} and (
            terms.position_effect != "replace"
            or terms.basis_policy != "allocate_cash_fraction"
            or terms.terminal
            or (policy == "stock_merger" and terms.cash_per_pre_event_unit != 0)
            or (policy == "stock_merger" and terms.cash_basis_fraction != 0)
            or (policy == "mixed_merger" and terms.cash_per_pre_event_unit <= 0)
            or (policy == "mixed_merger" and terms.cash_basis_fraction <= 0)
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_merger_shape_invalid"
            )
        if policy == "cash_exit" and (
            terms.position_effect != "close"
            or terms.cash_per_pre_event_unit < 0
            or terms.basis_policy != "close"
            or not terms.terminal
        ):
            raise CorporateActionContractError(
                "corporate_action.accounting_terms_cash_exit_shape_invalid"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_version_id": self.event_version_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type,
            "effective_at": self.effective_at,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "source_content_hash": self.source_content_hash,
            **(
                {"embedded_event_material_hash": (self.embedded_event_material_hash)}
                if self.schema_version == 2
                else {}
            ),
            "ratio": decimal_text(self.ratio) if self.ratio is not None else None,
            "cash_amount": (
                decimal_text(self.cash_amount) if self.cash_amount is not None else None
            ),
            "cash_currency": self.cash_currency,
            "replacement_symbol": self.replacement_symbol,
            "replacement_instrument_id": self.replacement_instrument_id,
            "tradability": self.tradability,
            **(
                {"accounting_terms": self.accounting_terms.as_dict()}
                if self.schema_version == 2 and self.accounting_terms is not None
                else {}
            ),
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="corporate_action_event")

    def is_known_at(self, as_of: str) -> bool:
        return _timestamp(
            self.observed_at, "corporate_action.observed_at"
        ) <= _timestamp(as_of, "corporate_action.as_of")

    def is_effective_at(self, as_of: str) -> bool:
        return _timestamp(
            self.effective_at, "corporate_action.effective_at"
        ) <= _timestamp(as_of, "corporate_action.as_of")


@dataclass(frozen=True, slots=True)
class CorporateActionSet:
    schema_version: int
    instrument_id: str
    action_set_id: str
    events: tuple[CorporateActionEvent, ...]

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CORPORATE_ACTION_SCHEMA_VERSIONS:
            raise CorporateActionContractError(
                "corporate_action_set_schema_unsupported"
            )
        if not _INSTRUMENT_ID.fullmatch(self.instrument_id):
            raise CorporateActionContractError(
                "corporate_action_set.instrument_id_invalid"
            )
        if not re.fullmatch(r"^cas_[a-z0-9][a-z0-9_-]{7,63}$", self.action_set_id):
            raise CorporateActionContractError(
                "corporate_action_set.action_set_id_invalid"
            )
        identities = [(item.event_id, item.event_version_id) for item in self.events]
        if len(identities) != len(set(identities)):
            raise CorporateActionContractError("corporate_action_set_duplicate_event")
        if self.schema_version == 1 and any(
            item.instrument_id != self.instrument_id for item in self.events
        ):
            raise CorporateActionContractError(
                "corporate_action_set_instrument_mismatch"
            )
        if any(item.schema_version != self.schema_version for item in self.events):
            raise CorporateActionContractError(
                "corporate_action_set_event_schema_mismatch"
            )
        version_ids = [item.event_version_id for item in self.events]
        if len(version_ids) != len(set(version_ids)):
            raise CorporateActionContractError(
                "corporate_action_set_duplicate_event_version_id"
            )
        versions_by_event: dict[str, list[CorporateActionEvent]] = {}
        for item in self.events:
            versions_by_event.setdefault(item.event_id, []).append(item)
        for versions in versions_by_event.values():
            canonical_versions = sorted(versions, key=lambda item: item.version)
            if [item.version for item in canonical_versions] != list(
                range(1, len(canonical_versions) + 1)
            ):
                raise CorporateActionContractError(
                    "corporate_action_event_versions_must_be_contiguous"
                )
            observed_times = [
                _timestamp(item.observed_at, "corporate_action.observed_at")
                for item in canonical_versions
            ]
            if any(
                later <= earlier
                for earlier, later in zip(observed_times, observed_times[1:])
            ):
                raise CorporateActionContractError(
                    "corporate_action_correction_not_observed_later"
                )
            if len({item.event_type for item in canonical_versions}) != 1:
                raise CorporateActionContractError(
                    "corporate_action_correction_event_type_changed"
                )
        ordered = tuple(
            sorted(
                self.events,
                key=lambda item: (
                    item.effective_at,
                    item.observed_at,
                    item.event_id,
                    item.version,
                ),
            )
        )
        if ordered != self.events:
            raise CorporateActionContractError("corporate_action_set_not_canonical")
        if self.schema_version == 2:
            active_instruments = {self.instrument_id}
            identity_ordered = sorted(
                ordered,
                key=lambda item: (
                    item.effective_at,
                    (
                        item.accounting_terms.same_timestamp_sequence
                        if item.accounting_terms is not None
                        else 0
                    ),
                    item.event_id,
                    item.version,
                ),
            )
            for item in identity_ordered:
                if item.instrument_id not in active_instruments:
                    raise CorporateActionContractError(
                        "corporate_action_set_identity_transition_unbound"
                    )
                terms = item.accounting_terms
                if (
                    terms is not None
                    and terms.position_effect == "replace"
                    and item.replacement_instrument_id is not None
                ):
                    active_instruments.add(item.replacement_instrument_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "instrument_id": self.instrument_id,
            "action_set_id": self.action_set_id,
            "events": [item.as_dict() for item in self.events],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="corporate_action_set")

    def causally_available(self, *, as_of: str) -> tuple[CorporateActionEvent, ...]:
        return tuple(item for item in self.events if item.is_known_at(as_of))

    def effective_and_known(self, *, as_of: str) -> tuple[CorporateActionEvent, ...]:
        return tuple(
            item
            for item in self.events
            if item.is_known_at(as_of) and item.is_effective_at(as_of)
        )

    def latest_effective_and_known(
        self, *, as_of: str
    ) -> tuple[CorporateActionEvent, ...]:
        """Return one causally available version per event identity.

        All versions remain in ``events`` for audit.  Transformations select
        only the latest correction observed by ``as_of`` so a corrected event
        is never applied twice and a future correction cannot leak backward.
        """

        latest_known: dict[str, CorporateActionEvent] = {}
        for item in self.events:
            if not item.is_known_at(as_of):
                continue
            current = latest_known.get(item.event_id)
            if current is None or item.version > current.version:
                latest_known[item.event_id] = item
        return tuple(
            sorted(
                (item for item in latest_known.values() if item.is_effective_at(as_of)),
                key=lambda item: (item.effective_at, item.event_id, item.version),
            )
        )


@dataclass(frozen=True, slots=True)
class AdjustmentPolicy:
    schema_version: int
    policy_id: str
    version: int
    price_series: str
    price_adjustment: str
    volume_adjustment: str
    dividend_treatment: str
    action_set_hash: str

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CORPORATE_ACTION_SCHEMA_VERSIONS:
            raise CorporateActionContractError("adjustment_policy_schema_unsupported")
        if not _POLICY_ID.fullmatch(self.policy_id):
            raise CorporateActionContractError("adjustment_policy.policy_id_invalid")
        if isinstance(self.version, bool) or self.version < 1:
            raise CorporateActionContractError("adjustment_policy.version_invalid")
        if self.price_series not in {"raw", "pre_adjusted"}:
            raise CorporateActionContractError("adjustment_policy.price_series_unknown")
        if self.price_adjustment not in {
            "none",
            "backward_split_only",
            "backward_total_return",
        }:
            raise CorporateActionContractError(
                "adjustment_policy.price_adjustment_unknown"
            )
        if self.volume_adjustment not in {"none", "inverse_split_factor"}:
            raise CorporateActionContractError(
                "adjustment_policy.volume_adjustment_unknown"
            )
        if self.dividend_treatment not in {
            "cash_flow_separate",
            "included_in_total_return_adjustment",
            "excluded",
        }:
            raise CorporateActionContractError(
                "adjustment_policy.dividend_treatment_unknown"
            )
        if self.price_series == "raw" and self.price_adjustment != "none":
            raise CorporateActionContractError(
                "adjustment_policy_raw_prices_cannot_claim_adjustment"
            )
        if self.price_series == "pre_adjusted" and self.price_adjustment == "none":
            raise CorporateActionContractError(
                "adjustment_policy_pre_adjusted_method_required"
            )
        if (
            self.price_adjustment == "backward_total_return"
            and self.dividend_treatment != "included_in_total_return_adjustment"
        ):
            raise CorporateActionContractError(
                "adjustment_policy_total_return_requires_included_dividends"
            )
        if (
            self.dividend_treatment == "included_in_total_return_adjustment"
            and self.price_adjustment != "backward_total_return"
        ):
            raise CorporateActionContractError(
                "adjustment_policy_included_dividends_require_total_return"
            )
        if not _HASH.fullmatch(self.action_set_hash):
            raise CorporateActionContractError(
                "adjustment_policy.action_set_hash_invalid"
            )
        if self.schema_version == 2 and self.price_series != "raw":
            raise CorporateActionContractError(
                "adjustment_policy_schema_v2_requires_causal_raw_prices"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "price_series": self.price_series,
            "price_adjustment": self.price_adjustment,
            "volume_adjustment": self.volume_adjustment,
            "dividend_treatment": self.dividend_treatment,
            "action_set_hash": self.action_set_hash,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="corporate_action_policy")


@dataclass(frozen=True, slots=True)
class CorporateActionOhlcv:
    """Exact raw or adjusted OHLCV row used by transformation evidence."""

    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        _timestamp(self.timestamp, "corporate_action_ohlcv.timestamp")
        for field, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
            ("volume", self.volume),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise CorporateActionContractError(
                    f"corporate_action_ohlcv.{field}_finite_decimal_required"
                )
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise CorporateActionContractError(
                "corporate_action_ohlcv_price_must_be_positive"
            )
        if self.volume < 0:
            raise CorporateActionContractError(
                "corporate_action_ohlcv_volume_must_be_nonnegative"
            )
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise CorporateActionContractError("corporate_action_ohlcv_bounds_invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "open": decimal_text(self.open),
            "high": decimal_text(self.high),
            "low": decimal_text(self.low),
            "close": decimal_text(self.close),
            "volume": decimal_text(self.volume),
        }


@dataclass(frozen=True, slots=True)
class CorporateActionApplicationEvidence:
    event_id: str
    event_version_id: str
    version: int
    event_type: str
    effective_at: str
    published_at: str
    observed_at: str
    source_content_hash: str
    price_factor: Decimal
    volume_factor: Decimal
    affected_row_count: int
    reference_close: Decimal | None
    rows_hash_before: str
    rows_hash_after: str

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_version_id": self.event_version_id,
            "version": self.version,
            "event_type": self.event_type,
            "effective_at": self.effective_at,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "source_content_hash": self.source_content_hash,
            "price_factor": decimal_text(self.price_factor),
            "volume_factor": decimal_text(self.volume_factor),
            "affected_row_count": self.affected_row_count,
            "reference_close": (
                decimal_text(self.reference_close)
                if self.reference_close is not None
                else None
            ),
            "rows_hash_before": self.rows_hash_before,
            "rows_hash_after": self.rows_hash_after,
        }


@dataclass(frozen=True, slots=True)
class CorporateActionTransformationResult:
    schema_version: int
    rows: tuple[CorporateActionOhlcv, ...]
    known_at: str
    input_rows_hash: str
    output_rows_hash: str
    action_set_hash: str
    adjustment_policy_hash: str
    input_series: str
    output_series: str
    applications: tuple[CorporateActionApplicationEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        material: dict[str, object] = {
            "schema_version": self.schema_version,
            "artifact_type": "corporate_action_transformation_evidence",
            "known_at": self.known_at,
            "input_series": self.input_series,
            "output_series": self.output_series,
            "input_row_count": len(self.rows),
            "output_row_count": len(self.rows),
            "input_rows_hash": self.input_rows_hash,
            "output_rows_hash": self.output_rows_hash,
            "action_set_hash": self.action_set_hash,
            "adjustment_policy_hash": self.adjustment_policy_hash,
            "applications": [item.as_dict() for item in self.applications],
        }
        return {
            **material,
            "content_hash": sha256_prefixed(
                material, label="corporate_action_transformation_evidence"
            ),
        }


def transform_raw_ohlcv(
    rows: tuple[CorporateActionOhlcv, ...],
    *,
    action_set: CorporateActionSet,
    policy: AdjustmentPolicy,
    known_at: str,
) -> CorporateActionTransformationResult:
    """Deterministically derive and hash an adjusted view from raw OHLCV.

    Split ratio means post-action units per pre-action unit: a 2-for-1 split
    has ratio ``2`` and a 1-for-10 reverse split has ratio ``0.1``.  Backward
    total-return dividends use ``(prior_close - cash) / prior_close``.  Rows on
    or after a known delisting/liquidation fail closed instead of fabricating
    prices.  The function never mutates or overwrites its raw input.
    """

    if not rows:
        raise CorporateActionContractError("corporate_action_rows_required")
    _timestamp(known_at, "corporate_action_transform.known_at")
    row_times = [
        _timestamp(item.timestamp, "corporate_action_ohlcv.timestamp") for item in rows
    ]
    if any(later <= earlier for earlier, later in zip(row_times, row_times[1:])):
        raise CorporateActionContractError(
            "corporate_action_rows_not_strictly_chronological"
        )
    if policy.action_set_hash != action_set.contract_hash():
        raise CorporateActionContractError(
            "corporate_action_transform_policy_action_set_hash_mismatch"
        )
    selected_events = action_set.latest_effective_and_known(as_of=known_at)
    for event in selected_events:
        if event.event_type not in {"delisting", "etf_liquidation"}:
            continue
        effective = _timestamp(event.effective_at, "corporate_action.effective_at")
        if any(row_time >= effective for row_time in row_times):
            raise CorporateActionContractError(
                "corporate_action_post_delisting_observation"
            )

    input_hash = _ohlcv_rows_hash(rows)
    adjusted = rows
    applications: list[CorporateActionApplicationEvidence] = []
    if policy.price_series == "pre_adjusted":
        if policy.price_adjustment == "backward_total_return" and (
            policy.dividend_treatment != "included_in_total_return_adjustment"
        ):
            raise CorporateActionContractError(
                "corporate_action_total_return_requires_included_dividends"
            )
        for event in selected_events:
            factor: Decimal | None = None
            volume_factor = Decimal("1")
            reference_close: Decimal | None = None
            if event.event_type in {"split", "reverse_split", "stock_dividend"}:
                assert event.ratio is not None
                factor = Decimal("1") / event.ratio
                if policy.volume_adjustment == "inverse_split_factor":
                    volume_factor = event.ratio
            elif event.event_type in {"cash_dividend", "etf_distribution"}:
                if policy.price_adjustment != "backward_total_return":
                    continue
                assert event.cash_amount is not None
                effective = _timestamp(
                    event.effective_at, "corporate_action.effective_at"
                )
                prior = [
                    item
                    for item, row_time in zip(rows, row_times)
                    if row_time < effective
                ]
                if not prior:
                    raise CorporateActionContractError(
                        "corporate_action_dividend_reference_close_missing"
                    )
                reference_close = prior[-1].close
                if event.cash_amount >= reference_close:
                    raise CorporateActionContractError(
                        "corporate_action_dividend_factor_not_positive"
                    )
                factor = (reference_close - event.cash_amount) / reference_close
            if factor is None:
                continue
            before_hash = _ohlcv_rows_hash(adjusted)
            effective = _timestamp(event.effective_at, "corporate_action.effective_at")
            affected = 0
            transformed: list[CorporateActionOhlcv] = []
            for item in adjusted:
                if (
                    _timestamp(item.timestamp, "corporate_action_ohlcv.timestamp")
                    < effective
                ):
                    affected += 1
                    transformed.append(
                        CorporateActionOhlcv(
                            timestamp=item.timestamp,
                            open=item.open * factor,
                            high=item.high * factor,
                            low=item.low * factor,
                            close=item.close * factor,
                            volume=item.volume * volume_factor,
                        )
                    )
                else:
                    transformed.append(item)
            adjusted = tuple(transformed)
            after_hash = _ohlcv_rows_hash(adjusted)
            applications.append(
                CorporateActionApplicationEvidence(
                    event_id=event.event_id,
                    event_version_id=event.event_version_id,
                    version=event.version,
                    event_type=event.event_type,
                    effective_at=event.effective_at,
                    published_at=event.published_at,
                    observed_at=event.observed_at,
                    source_content_hash=event.source_content_hash,
                    price_factor=factor,
                    volume_factor=volume_factor,
                    affected_row_count=affected,
                    reference_close=reference_close,
                    rows_hash_before=before_hash,
                    rows_hash_after=after_hash,
                )
            )
    output_hash = _ohlcv_rows_hash(adjusted)
    return CorporateActionTransformationResult(
        schema_version=action_set.schema_version,
        rows=adjusted,
        known_at=known_at,
        input_rows_hash=input_hash,
        output_rows_hash=output_hash,
        action_set_hash=action_set.contract_hash(),
        adjustment_policy_hash=policy.contract_hash(),
        input_series="raw",
        output_series=policy.price_series,
        applications=tuple(applications),
    )


def _ohlcv_rows_hash(rows: tuple[CorporateActionOhlcv, ...]) -> str:
    return sha256_prefixed(
        [item.as_dict() for item in rows], label="corporate_action_ohlcv_rows"
    )


def empty_action_set(instrument_id: str) -> CorporateActionSet:
    suffix = sha256_prefixed(
        {"instrument_id": instrument_id}, label="empty_corporate_action_set"
    ).split(":", 1)[1][:24]
    return CorporateActionSet(1, instrument_id, f"cas_{suffix}", ())


def raw_adjustment_policy(action_set: CorporateActionSet) -> AdjustmentPolicy:
    return AdjustmentPolicy(
        schema_version=1,
        policy_id="cap_raw_prices_v1",
        version=1,
        price_series="raw",
        price_adjustment="none",
        volume_adjustment="none",
        dividend_treatment="cash_flow_separate",
        action_set_hash=action_set.contract_hash(),
    )


def parse_corporate_action_set(
    value: object, *, expected_instrument_id: str
) -> CorporateActionSet:
    payload = _object(value, "corporate_action_set")
    _unknown(
        payload,
        {"schema_version", "instrument_id", "action_set_id", "events"},
        "corporate_action_set",
    )
    events_value = payload.get("events")
    if not isinstance(events_value, list):
        raise CorporateActionContractError("corporate_action_set.events_must_be_array")
    result = CorporateActionSet(
        schema_version=_integer(
            payload.get("schema_version"), "corporate_action_set.schema_version"
        ),
        instrument_id=_text(
            payload.get("instrument_id"), "corporate_action_set.instrument_id"
        ),
        action_set_id=_text(
            payload.get("action_set_id"), "corporate_action_set.action_set_id"
        ),
        events=tuple(_parse_event(item) for item in events_value),
    )
    if result.instrument_id != expected_instrument_id:
        raise CorporateActionContractError(
            "corporate_action_set_expected_instrument_mismatch"
        )
    return result


def parse_adjustment_policy(
    value: object, *, action_set: CorporateActionSet
) -> AdjustmentPolicy:
    payload = _object(value, "corporate_action_policy")
    _unknown(
        payload,
        {
            "schema_version",
            "policy_id",
            "version",
            "price_series",
            "price_adjustment",
            "volume_adjustment",
            "dividend_treatment",
            "action_set_hash",
        },
        "corporate_action_policy",
    )
    result = AdjustmentPolicy(
        schema_version=_integer(
            payload.get("schema_version"), "corporate_action_policy.schema_version"
        ),
        policy_id=_text(payload.get("policy_id"), "corporate_action_policy.policy_id"),
        version=_integer(payload.get("version"), "corporate_action_policy.version"),
        price_series=_text(
            payload.get("price_series"), "corporate_action_policy.price_series"
        ),
        price_adjustment=_text(
            payload.get("price_adjustment"),
            "corporate_action_policy.price_adjustment",
        ),
        volume_adjustment=_text(
            payload.get("volume_adjustment"),
            "corporate_action_policy.volume_adjustment",
        ),
        dividend_treatment=_text(
            payload.get("dividend_treatment"),
            "corporate_action_policy.dividend_treatment",
        ),
        action_set_hash=_text(
            payload.get("action_set_hash"),
            "corporate_action_policy.action_set_hash",
        ),
    )
    if result.action_set_hash != action_set.contract_hash():
        raise CorporateActionContractError(
            "corporate_action_policy_action_set_hash_mismatch"
        )
    if result.schema_version != action_set.schema_version:
        raise CorporateActionContractError(
            "corporate_action_policy_action_set_schema_mismatch"
        )
    return result


def parse_corporate_action_event(value: object) -> CorporateActionEvent:
    """Parse one complete event without applying action-set defaults."""

    return _parse_event(value)


def _parse_event(value: object) -> CorporateActionEvent:
    payload = _object(value, "corporate_action_set.events[]")
    _unknown(
        payload,
        {
            "schema_version",
            "event_id",
            "event_version_id",
            "version",
            "instrument_id",
            "event_type",
            "effective_at",
            "published_at",
            "observed_at",
            "source_content_hash",
            "embedded_event_material_hash",
            "ratio",
            "cash_amount",
            "cash_currency",
            "replacement_symbol",
            "replacement_instrument_id",
            "tradability",
            "accounting_terms",
        },
        "corporate_action_set.events[]",
    )
    try:
        return CorporateActionEvent(
            schema_version=_integer(
                payload.get("schema_version"),
                "corporate_action_set.events[].schema_version",
            ),
            event_id=_text(
                payload.get("event_id"), "corporate_action_set.events[].event_id"
            ),
            event_version_id=_text(
                payload.get("event_version_id"),
                "corporate_action_set.events[].event_version_id",
            ),
            version=_integer(
                payload.get("version"), "corporate_action_set.events[].version"
            ),
            instrument_id=_text(
                payload.get("instrument_id"),
                "corporate_action_set.events[].instrument_id",
            ),
            event_type=_text(
                payload.get("event_type"),
                "corporate_action_set.events[].event_type",
            ),
            effective_at=_text(
                payload.get("effective_at"),
                "corporate_action_set.events[].effective_at",
            ),
            published_at=_text(
                payload.get("published_at"),
                "corporate_action_set.events[].published_at",
            ),
            observed_at=_text(
                payload.get("observed_at"),
                "corporate_action_set.events[].observed_at",
            ),
            source_content_hash=_text(
                payload.get("source_content_hash"),
                "corporate_action_set.events[].source_content_hash",
            ),
            embedded_event_material_hash=_optional_text(
                payload.get("embedded_event_material_hash"),
                "corporate_action_set.events[].embedded_event_material_hash",
            ),
            ratio=(
                decimal_value(
                    payload.get("ratio"), "corporate_action_set.events[].ratio"
                )
                if payload.get("ratio") is not None
                else None
            ),
            cash_amount=(
                decimal_value(
                    payload.get("cash_amount"),
                    "corporate_action_set.events[].cash_amount",
                )
                if payload.get("cash_amount") is not None
                else None
            ),
            cash_currency=_optional_text(
                payload.get("cash_currency"),
                "corporate_action_set.events[].cash_currency",
            ),
            replacement_symbol=_optional_text(
                payload.get("replacement_symbol"),
                "corporate_action_set.events[].replacement_symbol",
            ),
            replacement_instrument_id=_optional_text(
                payload.get("replacement_instrument_id"),
                "corporate_action_set.events[].replacement_instrument_id",
            ),
            tradability=_optional_text(
                payload.get("tradability"),
                "corporate_action_set.events[].tradability",
            ),
            accounting_terms=(
                _parse_accounting_terms(payload["accounting_terms"])
                if payload.get("accounting_terms") is not None
                else None
            ),
        )
    except InstrumentContractError as exc:
        raise CorporateActionContractError(str(exc)) from exc


def _parse_accounting_terms(value: object) -> CorporateActionAccountingTerms:
    payload = _object(value, "corporate_action_set.events[].accounting_terms")
    allowed = {
        "schema_version",
        "settlement_policy",
        "same_timestamp_sequence",
        "entitlement_basis",
        "position_effect",
        "position_ratio",
        "cash_per_pre_event_unit",
        "cash_currency",
        "tax_policy",
        "tax_rate",
        "basis_policy",
        "cash_basis_fraction",
        "fractional_policy",
        "cash_in_lieu_price",
        "cash_in_lieu_tax_rate",
        "terminal",
        "continuation_price_policy",
    }
    _unknown(payload, allowed, "corporate_action_set.events[].accounting_terms")
    missing = sorted(allowed - set(payload))
    if missing:
        raise CorporateActionContractError(
            "corporate_action_set.events[].accounting_terms_missing_fields:"
            + ",".join(missing)
        )
    return CorporateActionAccountingTerms(
        schema_version=_integer(
            payload["schema_version"],
            "corporate_action_set.events[].accounting_terms.schema_version",
        ),
        settlement_policy=_text(
            payload["settlement_policy"],
            "corporate_action_set.events[].accounting_terms.settlement_policy",
        ),
        same_timestamp_sequence=_integer(
            payload["same_timestamp_sequence"],
            "corporate_action_set.events[].accounting_terms.same_timestamp_sequence",
        ),
        entitlement_basis=_text(
            payload["entitlement_basis"],
            "corporate_action_set.events[].accounting_terms.entitlement_basis",
        ),
        position_effect=_text(
            payload["position_effect"],
            "corporate_action_set.events[].accounting_terms.position_effect",
        ),
        position_ratio=decimal_value(
            payload["position_ratio"],
            "corporate_action_set.events[].accounting_terms.position_ratio",
        ),
        cash_per_pre_event_unit=decimal_value(
            payload["cash_per_pre_event_unit"],
            "corporate_action_set.events[].accounting_terms.cash_per_pre_event_unit",
        ),
        cash_currency=_optional_text(
            payload["cash_currency"],
            "corporate_action_set.events[].accounting_terms.cash_currency",
        ),
        tax_policy=_text(
            payload["tax_policy"],
            "corporate_action_set.events[].accounting_terms.tax_policy",
        ),
        tax_rate=decimal_value(
            payload["tax_rate"],
            "corporate_action_set.events[].accounting_terms.tax_rate",
        ),
        basis_policy=_text(
            payload["basis_policy"],
            "corporate_action_set.events[].accounting_terms.basis_policy",
        ),
        cash_basis_fraction=decimal_value(
            payload["cash_basis_fraction"],
            "corporate_action_set.events[].accounting_terms.cash_basis_fraction",
        ),
        fractional_policy=_text(
            payload["fractional_policy"],
            "corporate_action_set.events[].accounting_terms.fractional_policy",
        ),
        cash_in_lieu_price=(
            decimal_value(
                payload["cash_in_lieu_price"],
                "corporate_action_set.events[].accounting_terms.cash_in_lieu_price",
            )
            if payload["cash_in_lieu_price"] is not None
            else None
        ),
        cash_in_lieu_tax_rate=decimal_value(
            payload["cash_in_lieu_tax_rate"],
            "corporate_action_set.events[].accounting_terms.cash_in_lieu_tax_rate",
        ),
        terminal=_boolean(
            payload["terminal"],
            "corporate_action_set.events[].accounting_terms.terminal",
        ),
        continuation_price_policy=_text(
            payload["continuation_price_policy"],
            "corporate_action_set.events[].accounting_terms.continuation_price_policy",
        ),
    )


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorporateActionContractError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorporateActionContractError(f"{field}_timezone_required")
    return parsed


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CorporateActionContractError(f"{field}_must_be_object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorporateActionContractError(f"{field}_required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorporateActionContractError(f"{field}_must_be_integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise CorporateActionContractError(f"{field}_must_be_boolean")
    return value


def _unknown(payload: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CorporateActionContractError(
            f"{field}_unknown_fields:{','.join(unknown)}"
        )
