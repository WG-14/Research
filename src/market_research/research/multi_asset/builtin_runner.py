"""Strict built-in execution boundary for complete offline multi-asset studies.

The lower-level application coordinator accepts runner protocols so product
engines can remain independently testable.  That seam is intentionally not an
external API.  This module is the public production boundary: input JSON may
select only the source-owned ``offline-authoritative-v1`` profile and carries
three already allowlisted derivative request transports.  It cannot name,
import, or inject a Python runner class.

The profile executes real point-in-time expression/corporate-action logic,
the futures and option application authorities, the multi-leg execution
authority, the common ledger/exposure engine, and the joint scenario engine.
Every trace hash is derived from those returned economic objects.
"""

from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence, cast

from market_research.paths import ResearchPathManager
from market_research.research.derivatives.application import (
    DerivativeResearchApplicationService,
    FuturesOrderCommand,
    FuturesRollCommand,
    FuturesSettlementCommand,
    FuturesStudyRequest,
    MultiLegStudyRequest,
    OptionStudyRequest,
)
from market_research.research.derivatives.application_codec import (
    DerivativeApplicationTransport,
)
from market_research.research.derivatives.futures import (
    ContinuousFuturesPoint,
    FuturesLedger,
    OrderSide as FuturesOrderSide,
    SimulationStep,
)
from market_research.research.derivatives.options import (
    BlackScholesModel,
    FillStatus,
    OptionGreeks,
    OptionLifecycleEvent,
    OptionPosition,
    PositionSide,
    QuoteState,
    SettlementType as DerivativeOptionSettlementType,
    ValuationInputSnapshot,
    mark_option_position,
    position_from_fill,
    simulate_option_fill,
    simulate_option_lifecycle,
)
from market_research.research.derivatives.workflow import (
    read_external_derivative_json,
    write_external_derivative_json,
)
from market_research.research.instrument_kinds import InstrumentKind
from market_research.research.multi_asset.accounting import (
    LedgerPnlReconciliation,
    ReportLedgerReconciliation,
    ReportPnlSummary,
    encode_report_payload,
    report_payload_hash,
)
from market_research.research.multi_asset.cards import (
    CardValidationResult,
    DataCard,
    DataFieldDefinition,
    DataFieldUnit,
    DataRowResolverMetadata,
    DataTemporalSemantics,
    DistributionStatus,
    ModelCard,
    ModelParameter,
    ValidationStatus,
)
from market_research.research.multi_asset.authoritative_inputs import (
    AuthoritativeInputError,
    AuthoritativeInputFactory,
    AuthoritativeInputReceipt,
    AuthoritativeOutputBinding,
)
from market_research.research.multi_asset.costs import (
    ExecutionContext,
    ExecutionSide,
    LinearExecutionCostModel,
)
from market_research.research.multi_asset.application import (
    IntegratedScenarioExecution,
    MultiAssetExperimentError,
    MultiAssetExperimentSpec,
    MultiAssetResearchApplicationService,
    MultiAssetResearchExecution,
    MultiAssetRunRequest,
    MultiAssetScenarioRunners,
    ScenarioRunContext,
    multi_asset_experiment_spec_from_dict,
)
from market_research.research.multi_asset.domain import (
    ContractSpecification,
    EconomicUnderlying,
    EffectivePeriod,
    Instrument,
    InstrumentRegistry,
    InstrumentRelationship,
    InstrumentRelationshipType,
    SettlementType,
    SourceReference,
)
from market_research.research.multi_asset.evidence import (
    ScenarioObjectHashes,
    evidence_hash,
    scenario_object_hashes,
    validated_study_report_payload,
)
from market_research.research.multi_asset.exposure import (
    ExposureEngine,
    ExposurePosition,
    OptionValuationAdapter,
    PortfolioExposureSnapshot,
)
from market_research.research.multi_asset.expression import (
    DEFAULT_EXPRESSION_POLICY,
    DesiredEconomicPayoff,
    Direction,
    EconomicHypothesis,
    ExecutionMode,
    ExpectedMarketDistribution,
    ExpressionCandidate,
    ExpressionKind,
    InstrumentChoice,
    InstrumentExpressionEngine,
    LegRole,
    LegSelectionRule,
    ProductKind,
    ScenarioRange,
    StrategyTargets,
)
from market_research.research.multi_asset.futures_path import (
    trace_continuous_signal,
)
from market_research.research.multi_asset.futures_delivery import (
    FuturesSettlementMode,
)
from market_research.research.multi_asset.market_state import (
    LiquidityQuote,
    MarketDataQuality,
    MarketState,
    ObservationMetadata,
    OptionAnalyticsMark,
    OptionChainState,
    OptionContractQuote,
    OptionRight,
    QuoteCondition,
    SpotQuote,
)
from market_research.research.multi_asset.multileg_execution import (
    LedgerExposureBinding,
    LedgerExposureRequest,
    MultiLegDisposition,
    MultiLegLedgerCommand,
    MultiLegLedgerExecutionService,
)
from market_research.research.multi_asset.option_analytics import (
    AuthoritativeOptionAnalyticsFactory,
    default_analytics_comparison_policy,
    default_option_model_registry,
)
from market_research.research.multi_asset.option_path import (
    CalculatedOptionDelta,
    DeltaFallback,
    ForwardEstimate,
    ForwardMethod,
    OptionChainCleaner,
    OptionCleaningPolicy,
    OptionAttributionPolicy,
    OptionGreeks as PathOptionGreeks,
    OptionPathMark,
    OptionRight as PathOptionRight,
    OptionSelectionPolicy,
    RawOptionObservation,
    attribute_option_path,
    select_option_contract,
)
from market_research.research.multi_asset.option_pricing import (
    BlackScholesPricingAdapter,
    black_scholes_pricing_specification,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    PortfolioSnapshot,
    PositionView,
    UnifiedPortfolioLedger,
    adapt_corporate_action_application,
    adapt_option_fill,
    adapt_option_lifecycle,
    cost_events_from_breakdown,
    funding_event,
    trade_event,
)
from market_research.research.multi_asset.public_spot_futures_profile import (
    PublicFuturesProfileReceipt,
    PublicSpotProfileReceipt,
    build_public_t01_inputs,
    build_public_t02_inputs,
    run_public_t01_spot_profile,
    run_public_t02_futures_profile,
)
from market_research.research.multi_asset.public_option_profile import (
    PublicOptionInstitutionalReceipt,
    build_public_t03_fixture_inputs,
    default_public_option_institutional_factory,
    run_public_option_profile,
)
from market_research.research.multi_asset.public_integrated_profile import (
    PublicIntegratedProfileReceipt,
    build_public_t04_fixture_inputs,
    run_public_integrated_profile,
)
from market_research.research.multi_asset.public_execution_evidence import (
    PublicExecutionEvidenceBundle,
    build_public_execution_evidence_bundle,
    publish_public_execution_evidence_bundle,
)
from market_research.research.multi_asset.public_package import (
    PublicPackageMaterials,
    build_public_validated_package_request,
)
from market_research.research.multi_asset.research_package import (
    ArtifactChecksum,
    BoundedEvidenceArtifactResolver,
    EvidenceArtifactRef,
    EvidenceArtifactRole,
    bytes_sha256,
)
from market_research.research.multi_asset.portable_source import (
    build_portable_engine_source_archive,
)
from market_research.research.multi_asset.scenarios import (
    JointMarketShock,
    JointScenarioEngine,
    ShockedMarketState,
)
from market_research.research.multi_asset.spot import (
    CashBalance as SpotCashBalance,
    CorporateAction,
    CorporateActionType,
    PointInTimeSpotUniverse,
    SpotBook,
    SpotPosition,
    UniverseMembership,
    apply_corporate_action,
)
from market_research.research.multi_asset.study import (
    FuturesScenarioTrace,
    FuturesSourceMapping,
    IntegratedLegResult,
    IntegratedScenarioTrace,
    OptionScenarioTrace,
    ScenarioAccounting,
    SpotScenarioTrace,
)
from market_research.research.multi_asset.validated_package import (
    build_validated_package,
)


BUILTIN_MULTI_ASSET_SCHEMA_VERSION = 1
BUILTIN_RUNNER_ID = "multi-asset-offline-authoritative"
BUILTIN_RUNNER_VERSION = "v1"
_ARTIFACT_TYPE = "offline_multi_asset_builtin_request"
_EXECUTION_ARTIFACT_TYPE = "offline_multi_asset_builtin_execution"
_REPRODUCTION_ARTIFACT_TYPE = "offline_multi_asset_builtin_reproduction"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "account",
        "account_id",
        "broker_account",
        "broker_api_key",
        "exchange_api_key",
        "exchange_api_secret",
        "order_route",
        "order_router",
        "order_submission",
        "private_exchange",
        "network_market_data",
        "network_market_data_collection",
        "market_data_collection",
        "deployment",
        "deployment_target",
        "live_account",
        "approved_for_live",
    }
)


class BuiltinMultiAssetCodecError(ValueError):
    """The public built-in request is malformed, unsafe, or hash-inconsistent."""


class BuiltinRunnerProfile(StrEnum):
    OFFLINE_AUTHORITATIVE_V1 = "offline-authoritative-v1"


class BuiltinReproductionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
        raise BuiltinMultiAssetCodecError(f"{field_name}_exact_decimal_required")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BuiltinMultiAssetCodecError(f"{field_name}_invalid_decimal") from exc
    if not parsed.is_finite() or (
        isinstance(value, str) and _decimal_text(parsed) != value
    ):
        raise BuiltinMultiAssetCodecError(f"{field_name}_noncanonical_decimal")
    return parsed


def _timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise BuiltinMultiAssetCodecError(f"{field_name}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BuiltinMultiAssetCodecError(f"{field_name}_timezone_required")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise BuiltinMultiAssetCodecError(f"{field_name}_invalid_hash")


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise BuiltinMultiAssetCodecError(f"{field_name}_invalid")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise BuiltinMultiAssetCodecError(f"{field_name}_object_required")
    return value


def _exact_fields(
    value: Mapping[str, object],
    expected: set[str],
    field_name: str,
) -> None:
    if set(value) != expected:
        raise BuiltinMultiAssetCodecError(f"{field_name}_fields_invalid")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise BuiltinMultiAssetCodecError(f"{field_name}_text_required")
    return value


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BuiltinMultiAssetCodecError(f"{field_name}_integer_required")
    return value


def _reject_unsafe(value: object, path: str) -> None:
    if isinstance(value, float):
        raise BuiltinMultiAssetCodecError(f"{path}_float_forbidden")
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = (
                re.sub(
                    r"[^a-z0-9]+",
                    "_",
                    re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)),
                )
                .strip("_")
                .lower()
            )
            if normalized in _FORBIDDEN_KEYS:
                raise BuiltinMultiAssetCodecError(
                    f"builtin_multi_asset_live_field_forbidden:{path}.{normalized}"
                )
            _reject_unsafe(child, f"{path}.{normalized}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_unsafe(child, f"{path}[{index}]")


def _identity_hash(payload: object, *, label: str) -> str:
    return evidence_hash(payload, label=label)


@dataclass(frozen=True, slots=True)
class BuiltinSpotScenarioInput:
    """Minimal exact inputs for the source-owned PIT spot scenario."""

    instrument_id: str
    economic_underlying_id: str
    currency: str
    decision_at: str
    knowledge_at: str
    quantity: Decimal
    entry_price: Decimal
    split_ratio: Decimal
    dividend_per_unit: Decimal
    dividend_tax_rate: Decimal
    commission_per_unit: Decimal

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "builtin_spot.instrument_id")
        _require_id(
            self.economic_underlying_id,
            "builtin_spot.economic_underlying_id",
        )
        if not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", self.currency):
            raise BuiltinMultiAssetCodecError("builtin_spot.currency_invalid")
        decision = _timestamp(self.decision_at, "builtin_spot.decision_at")
        knowledge = _timestamp(self.knowledge_at, "builtin_spot.knowledge_at")
        if knowledge > decision:
            raise BuiltinMultiAssetCodecError("builtin_spot_future_knowledge")
        for field_name in (
            "quantity",
            "entry_price",
            "split_ratio",
            "dividend_per_unit",
            "commission_per_unit",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise BuiltinMultiAssetCodecError(
                    f"builtin_spot.{field_name}_positive_decimal_required"
                )
        if (
            not isinstance(self.dividend_tax_rate, Decimal)
            or not self.dividend_tax_rate.is_finite()
            or not Decimal("0") <= self.dividend_tax_rate <= Decimal("1")
        ):
            raise BuiltinMultiAssetCodecError("builtin_spot.dividend_tax_rate_invalid")
        if self.commission_per_unit <= 0:
            raise BuiltinMultiAssetCodecError(
                "builtin_spot.commission_per_unit_positive_required"
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "instrument_id": self.instrument_id,
            "economic_underlying_id": self.economic_underlying_id,
            "currency": self.currency,
            "decision_at": self.decision_at,
            "knowledge_at": self.knowledge_at,
            "quantity": _decimal_text(self.quantity),
            "entry_price": _decimal_text(self.entry_price),
            "split_ratio": _decimal_text(self.split_ratio),
            "dividend_per_unit": _decimal_text(self.dividend_per_unit),
            "dividend_tax_rate": _decimal_text(self.dividend_tax_rate),
            "commission_per_unit": _decimal_text(self.commission_per_unit),
        }

    @classmethod
    def from_dict(cls, value: object) -> BuiltinSpotScenarioInput:
        payload = _mapping(value, "builtin_spot")
        expected = {
            "instrument_id",
            "economic_underlying_id",
            "currency",
            "decision_at",
            "knowledge_at",
            "quantity",
            "entry_price",
            "split_ratio",
            "dividend_per_unit",
            "dividend_tax_rate",
            "commission_per_unit",
        }
        _exact_fields(payload, expected, "builtin_spot")
        return cls(
            instrument_id=_text(payload["instrument_id"], "builtin_spot.instrument_id"),
            economic_underlying_id=_text(
                payload["economic_underlying_id"],
                "builtin_spot.economic_underlying_id",
            ),
            currency=_text(payload["currency"], "builtin_spot.currency"),
            decision_at=_text(payload["decision_at"], "builtin_spot.decision_at"),
            knowledge_at=_text(payload["knowledge_at"], "builtin_spot.knowledge_at"),
            quantity=_decimal(payload["quantity"], "builtin_spot.quantity"),
            entry_price=_decimal(payload["entry_price"], "builtin_spot.entry_price"),
            split_ratio=_decimal(payload["split_ratio"], "builtin_spot.split_ratio"),
            dividend_per_unit=_decimal(
                payload["dividend_per_unit"],
                "builtin_spot.dividend_per_unit",
            ),
            dividend_tax_rate=_decimal(
                payload["dividend_tax_rate"],
                "builtin_spot.dividend_tax_rate",
            ),
            commission_per_unit=_decimal(
                payload["commission_per_unit"],
                "builtin_spot.commission_per_unit",
            ),
        )


@dataclass(frozen=True, slots=True)
class BuiltinEconomicScenarioPolicy:
    """Every economic or selection knob used by the fixed runner profile."""

    continuous_series_id: str
    expected_return: Decimal
    annualized_volatility: Decimal
    downside_tail_return: Decimal
    upside_return: Decimal
    horizon_days: int
    futures_signal_return_threshold: Decimal
    futures_target_notional: Decimal
    option_target_days_to_expiry: int
    option_minimum_days_to_expiry: int
    option_maximum_days_to_expiry: int
    option_target_delta: Decimal
    option_maximum_delta_distance: Decimal
    option_minimum_liquidity_weight: Decimal
    option_maximum_absolute_residual: Decimal
    option_maximum_relative_residual: Decimal
    joint_spot_return: Decimal
    joint_liquidity_haircut: Decimal
    joint_liquidity_cost_multiplier: Decimal
    joint_margin_multiplier: Decimal

    def __post_init__(self) -> None:
        _require_id(
            self.continuous_series_id,
            "builtin_policy.continuous_series_id",
        )
        for field_name in (
            "expected_return",
            "annualized_volatility",
            "downside_tail_return",
            "upside_return",
            "futures_signal_return_threshold",
            "futures_target_notional",
            "option_target_delta",
            "option_maximum_delta_distance",
            "option_minimum_liquidity_weight",
            "option_maximum_absolute_residual",
            "option_maximum_relative_residual",
            "joint_spot_return",
            "joint_liquidity_haircut",
            "joint_liquidity_cost_multiplier",
            "joint_margin_multiplier",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise BuiltinMultiAssetCodecError(
                    f"builtin_policy.{field_name}_exact_decimal_required"
                )
        if self.annualized_volatility <= 0:
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.annualized_volatility_positive_required"
            )
        if not Decimal("-1") < self.downside_tail_return <= self.expected_return:
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.downside_tail_return_invalid"
            )
        if self.upside_return < self.expected_return:
            raise BuiltinMultiAssetCodecError("builtin_policy.upside_return_invalid")
        if (
            isinstance(self.horizon_days, bool)
            or self.horizon_days <= 0
            or isinstance(self.option_target_days_to_expiry, bool)
            or isinstance(self.option_minimum_days_to_expiry, bool)
            or isinstance(self.option_maximum_days_to_expiry, bool)
            or not (
                0
                <= self.option_minimum_days_to_expiry
                <= self.option_target_days_to_expiry
                <= self.option_maximum_days_to_expiry
            )
        ):
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.horizon_or_expiry_window_invalid"
            )
        if self.futures_signal_return_threshold < 0:
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.futures_signal_return_threshold_invalid"
            )
        if self.futures_target_notional <= 0:
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.futures_target_notional_positive_required"
            )
        if not Decimal("-1") <= self.option_target_delta <= Decimal("1"):
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.option_target_delta_invalid"
            )
        if not Decimal("0") <= self.option_maximum_delta_distance <= Decimal("2"):
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.option_maximum_delta_distance_invalid"
            )
        if not Decimal("0") <= self.option_minimum_liquidity_weight <= Decimal("1"):
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.option_minimum_liquidity_weight_invalid"
            )
        if self.option_maximum_absolute_residual <= 0 or not Decimal(
            "0"
        ) < self.option_maximum_relative_residual <= Decimal("0.25"):
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.option_attribution_tolerance_invalid"
            )
        if self.joint_spot_return <= Decimal("-1") or self.joint_spot_return == 0:
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.joint_spot_return_invalid"
            )
        if not Decimal("0") <= self.joint_liquidity_haircut < Decimal("1"):
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.joint_liquidity_haircut_invalid"
            )
        if (
            self.joint_liquidity_cost_multiplier <= 0
            or self.joint_margin_multiplier <= 0
        ):
            raise BuiltinMultiAssetCodecError(
                "builtin_policy.joint_multipliers_positive_required"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "continuous_series_id": self.continuous_series_id,
            "expected_return": _decimal_text(self.expected_return),
            "annualized_volatility": _decimal_text(self.annualized_volatility),
            "downside_tail_return": _decimal_text(self.downside_tail_return),
            "upside_return": _decimal_text(self.upside_return),
            "horizon_days": self.horizon_days,
            "futures_signal_return_threshold": _decimal_text(
                self.futures_signal_return_threshold
            ),
            "futures_target_notional": _decimal_text(self.futures_target_notional),
            "option_target_days_to_expiry": (self.option_target_days_to_expiry),
            "option_minimum_days_to_expiry": (self.option_minimum_days_to_expiry),
            "option_maximum_days_to_expiry": (self.option_maximum_days_to_expiry),
            "option_target_delta": _decimal_text(self.option_target_delta),
            "option_maximum_delta_distance": _decimal_text(
                self.option_maximum_delta_distance
            ),
            "option_minimum_liquidity_weight": _decimal_text(
                self.option_minimum_liquidity_weight
            ),
            "option_maximum_absolute_residual": _decimal_text(
                self.option_maximum_absolute_residual
            ),
            "option_maximum_relative_residual": _decimal_text(
                self.option_maximum_relative_residual
            ),
            "joint_spot_return": _decimal_text(self.joint_spot_return),
            "joint_liquidity_haircut": _decimal_text(self.joint_liquidity_haircut),
            "joint_liquidity_cost_multiplier": _decimal_text(
                self.joint_liquidity_cost_multiplier
            ),
            "joint_margin_multiplier": _decimal_text(self.joint_margin_multiplier),
        }

    @classmethod
    def from_dict(cls, value: object) -> BuiltinEconomicScenarioPolicy:
        payload = _mapping(value, "builtin_policy")
        integer_fields = {
            "horizon_days",
            "option_target_days_to_expiry",
            "option_minimum_days_to_expiry",
            "option_maximum_days_to_expiry",
        }
        decimal_fields = {
            "expected_return",
            "annualized_volatility",
            "downside_tail_return",
            "upside_return",
            "futures_signal_return_threshold",
            "futures_target_notional",
            "option_target_delta",
            "option_maximum_delta_distance",
            "option_minimum_liquidity_weight",
            "option_maximum_absolute_residual",
            "option_maximum_relative_residual",
            "joint_spot_return",
            "joint_liquidity_haircut",
            "joint_liquidity_cost_multiplier",
            "joint_margin_multiplier",
        }
        _exact_fields(
            payload,
            {"continuous_series_id", *integer_fields, *decimal_fields},
            "builtin_policy",
        )
        return cls(
            continuous_series_id=_text(
                payload["continuous_series_id"],
                "builtin_policy.continuous_series_id",
            ),
            expected_return=_decimal(
                payload["expected_return"],
                "builtin_policy.expected_return",
            ),
            annualized_volatility=_decimal(
                payload["annualized_volatility"],
                "builtin_policy.annualized_volatility",
            ),
            downside_tail_return=_decimal(
                payload["downside_tail_return"],
                "builtin_policy.downside_tail_return",
            ),
            upside_return=_decimal(
                payload["upside_return"],
                "builtin_policy.upside_return",
            ),
            horizon_days=_integer(
                payload["horizon_days"],
                "builtin_policy.horizon_days",
            ),
            futures_signal_return_threshold=_decimal(
                payload["futures_signal_return_threshold"],
                "builtin_policy.futures_signal_return_threshold",
            ),
            futures_target_notional=_decimal(
                payload["futures_target_notional"],
                "builtin_policy.futures_target_notional",
            ),
            option_target_days_to_expiry=_integer(
                payload["option_target_days_to_expiry"],
                "builtin_policy.option_target_days_to_expiry",
            ),
            option_minimum_days_to_expiry=_integer(
                payload["option_minimum_days_to_expiry"],
                "builtin_policy.option_minimum_days_to_expiry",
            ),
            option_maximum_days_to_expiry=_integer(
                payload["option_maximum_days_to_expiry"],
                "builtin_policy.option_maximum_days_to_expiry",
            ),
            option_target_delta=_decimal(
                payload["option_target_delta"],
                "builtin_policy.option_target_delta",
            ),
            option_maximum_delta_distance=_decimal(
                payload["option_maximum_delta_distance"],
                "builtin_policy.option_maximum_delta_distance",
            ),
            option_minimum_liquidity_weight=_decimal(
                payload["option_minimum_liquidity_weight"],
                "builtin_policy.option_minimum_liquidity_weight",
            ),
            option_maximum_absolute_residual=_decimal(
                payload["option_maximum_absolute_residual"],
                "builtin_policy.option_maximum_absolute_residual",
            ),
            option_maximum_relative_residual=_decimal(
                payload["option_maximum_relative_residual"],
                "builtin_policy.option_maximum_relative_residual",
            ),
            joint_spot_return=_decimal(
                payload["joint_spot_return"],
                "builtin_policy.joint_spot_return",
            ),
            joint_liquidity_haircut=_decimal(
                payload["joint_liquidity_haircut"],
                "builtin_policy.joint_liquidity_haircut",
            ),
            joint_liquidity_cost_multiplier=_decimal(
                payload["joint_liquidity_cost_multiplier"],
                "builtin_policy.joint_liquidity_cost_multiplier",
            ),
            joint_margin_multiplier=_decimal(
                payload["joint_margin_multiplier"],
                "builtin_policy.joint_margin_multiplier",
            ),
        )


def _continuous_futures_point_from_dict(
    value: object,
) -> ContinuousFuturesPoint:
    payload = _mapping(value, "builtin_futures_signal_point")
    _exact_fields(
        payload,
        {
            "schema_version",
            "point_id",
            "series_id",
            "root_id",
            "observed_at",
            "source_contract_id",
            "source_quote_hash",
            "source_price",
            "continuous_price",
            "additive_adjustment",
            "multiplicative_adjustment",
            "roll_gap",
            "policy_hash",
            "roll_decision_hash",
            "chain_snapshot_hash",
            "previous_point_hash",
            "signal_only",
            "content_hash",
        },
        "builtin_futures_signal_point",
    )
    previous_raw = payload["previous_point_hash"]
    if previous_raw is not None and not isinstance(previous_raw, str):
        raise BuiltinMultiAssetCodecError(
            "builtin_futures_signal_point.previous_point_hash_invalid"
        )
    if payload["signal_only"] is not True:
        raise BuiltinMultiAssetCodecError(
            "builtin_futures_signal_point_must_be_signal_only"
        )
    point = ContinuousFuturesPoint(
        schema_version=_integer(
            payload["schema_version"],
            "builtin_futures_signal_point.schema_version",
        ),
        point_id=_text(
            payload["point_id"],
            "builtin_futures_signal_point.point_id",
        ),
        series_id=_text(
            payload["series_id"],
            "builtin_futures_signal_point.series_id",
        ),
        root_id=_text(
            payload["root_id"],
            "builtin_futures_signal_point.root_id",
        ),
        observed_at=_text(
            payload["observed_at"],
            "builtin_futures_signal_point.observed_at",
        ),
        source_contract_id=_text(
            payload["source_contract_id"],
            "builtin_futures_signal_point.source_contract_id",
        ),
        source_quote_hash=_text(
            payload["source_quote_hash"],
            "builtin_futures_signal_point.source_quote_hash",
        ),
        source_price=_decimal(
            payload["source_price"],
            "builtin_futures_signal_point.source_price",
        ),
        continuous_price=_decimal(
            payload["continuous_price"],
            "builtin_futures_signal_point.continuous_price",
        ),
        additive_adjustment=_decimal(
            payload["additive_adjustment"],
            "builtin_futures_signal_point.additive_adjustment",
        ),
        multiplicative_adjustment=_decimal(
            payload["multiplicative_adjustment"],
            "builtin_futures_signal_point.multiplicative_adjustment",
        ),
        roll_gap=_decimal(
            payload["roll_gap"],
            "builtin_futures_signal_point.roll_gap",
        ),
        policy_hash=_text(
            payload["policy_hash"],
            "builtin_futures_signal_point.policy_hash",
        ),
        roll_decision_hash=_text(
            payload["roll_decision_hash"],
            "builtin_futures_signal_point.roll_decision_hash",
        ),
        chain_snapshot_hash=_text(
            payload["chain_snapshot_hash"],
            "builtin_futures_signal_point.chain_snapshot_hash",
        ),
        previous_point_hash=previous_raw,
        signal_only=True,
    )
    if point.content_hash != _text(
        payload["content_hash"],
        "builtin_futures_signal_point.content_hash",
    ):
        raise BuiltinMultiAssetCodecError(
            "builtin_futures_signal_point_content_hash_mismatch"
        )
    return point


@dataclass(frozen=True, slots=True)
class BuiltinScenarioInputs:
    spot: BuiltinSpotScenarioInput
    policy: BuiltinEconomicScenarioPolicy
    futures_signal_points: tuple[ContinuousFuturesPoint, ...]
    futures_request: DerivativeApplicationTransport
    option_request: DerivativeApplicationTransport
    option_intermediate_request: DerivativeApplicationTransport
    multi_leg_request: DerivativeApplicationTransport
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.futures_request.payload) is not FuturesStudyRequest:
            raise BuiltinMultiAssetCodecError("builtin_futures_request_required")
        if len(self.futures_signal_points) < 3:
            raise BuiltinMultiAssetCodecError(
                "builtin_futures_signal_points_insufficient"
            )
        futures_request = self.futures_request.payload
        signal_trace = trace_continuous_signal(
            self.futures_signal_points,
            trace_id="builtin-input-validation-trace",
        )
        if (
            signal_trace.series_id != self.policy.continuous_series_id
            or signal_trace.root_id != futures_request.chain.root_id
            or any(
                item.chain_snapshot_hash != futures_request.chain.content_hash
                for item in self.futures_signal_points
            )
        ):
            raise BuiltinMultiAssetCodecError(
                "builtin_futures_signal_request_binding_mismatch"
            )
        if type(self.option_request.payload) is not OptionStudyRequest:
            raise BuiltinMultiAssetCodecError("builtin_option_request_required")
        if type(self.option_intermediate_request.payload) is not OptionStudyRequest:
            raise BuiltinMultiAssetCodecError(
                "builtin_option_intermediate_request_required"
            )
        if type(self.multi_leg_request.payload) is not MultiLegStudyRequest:
            raise BuiltinMultiAssetCodecError("builtin_multileg_request_required")
        option = self.option_request.payload
        intermediate = self.option_intermediate_request.payload
        multi_leg = self.multi_leg_request.payload
        if (
            option.chain.underlying_id != self.spot.instrument_id
            or intermediate.chain.underlying_id != self.spot.instrument_id
            or multi_leg.chain.underlying_id != self.spot.instrument_id
        ):
            raise BuiltinMultiAssetCodecError("builtin_spot_option_underlying_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _identity_hash(
                self.identity_payload(),
                label="builtin-multi-asset-scenario-inputs",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "spot": self.spot.as_dict(),
            "policy": self.policy.as_dict(),
            "futures_signal_points": [
                item.as_dict() for item in self.futures_signal_points
            ],
            "futures_request": self.futures_request.as_dict(),
            "option_request": self.option_request.as_dict(),
            "option_intermediate_request": (self.option_intermediate_request.as_dict()),
            "multi_leg_request": self.multi_leg_request.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> BuiltinScenarioInputs:
        payload = _mapping(value, "builtin_inputs")
        _exact_fields(
            payload,
            {
                "spot",
                "policy",
                "futures_signal_points",
                "futures_request",
                "option_request",
                "option_intermediate_request",
                "multi_leg_request",
                "content_hash",
            },
            "builtin_inputs",
        )
        raw_points = payload["futures_signal_points"]
        if not isinstance(raw_points, list):
            raise BuiltinMultiAssetCodecError(
                "builtin_futures_signal_points_array_required"
            )
        result = cls(
            spot=BuiltinSpotScenarioInput.from_dict(payload["spot"]),
            policy=BuiltinEconomicScenarioPolicy.from_dict(payload["policy"]),
            futures_signal_points=tuple(
                _continuous_futures_point_from_dict(item) for item in raw_points
            ),
            futures_request=DerivativeApplicationTransport.from_dict(
                payload["futures_request"]
            ),
            option_request=DerivativeApplicationTransport.from_dict(
                payload["option_request"]
            ),
            option_intermediate_request=(
                DerivativeApplicationTransport.from_dict(
                    payload["option_intermediate_request"]
                )
            ),
            multi_leg_request=DerivativeApplicationTransport.from_dict(
                payload["multi_leg_request"]
            ),
        )
        expected = _text(payload["content_hash"], "builtin_inputs.content_hash")
        if result.content_hash != expected:
            raise BuiltinMultiAssetCodecError("builtin_inputs_content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class BuiltinMultiAssetRequest:
    run_id: str
    spec: MultiAssetExperimentSpec
    evidence_references: tuple[EvidenceArtifactRef, ...]
    inputs: BuiltinScenarioInputs
    profile: BuiltinRunnerProfile = BuiltinRunnerProfile.OFFLINE_AUTHORITATIVE_V1
    content_hash: str = field(init=False)
    schema_version: int = BUILTIN_MULTI_ASSET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BUILTIN_MULTI_ASSET_SCHEMA_VERSION:
            raise BuiltinMultiAssetCodecError("builtin_multi_asset_schema_unsupported")
        _require_id(self.run_id, "builtin_request.run_id")
        if self.profile is not BuiltinRunnerProfile.OFFLINE_AUTHORITATIVE_V1:
            raise BuiltinMultiAssetCodecError("builtin_multi_asset_profile_unsupported")
        if not self.evidence_references:
            raise BuiltinMultiAssetCodecError("builtin_multi_asset_evidence_required")
        identities = tuple(
            (
                scenario.runner_id,
                scenario.runner_version,
                dict(scenario.parameters),
            )
            for scenario in self.spec.scenarios[:4]
        )
        expected = tuple(
            (
                BUILTIN_RUNNER_ID,
                BUILTIN_RUNNER_VERSION,
                {"builtin_inputs_hash": self.inputs.content_hash},
            )
            for _ in range(4)
        )
        if identities != expected:
            raise BuiltinMultiAssetCodecError(
                "builtin_multi_asset_spec_runner_binding_mismatch"
            )
        actual_ids = {
            self.inputs.spot.instrument_id,
            *(
                item.contract_id
                for item in cast(
                    FuturesStudyRequest,
                    self.inputs.futures_request.payload,
                ).chain.contracts
            ),
            *(
                item.contract_id
                for item in cast(
                    OptionStudyRequest,
                    self.inputs.option_request.payload,
                ).chain.contracts
            ),
            *(
                item.contract_id
                for item in cast(
                    OptionStudyRequest,
                    self.inputs.option_intermediate_request.payload,
                ).chain.contracts
            ),
            *(
                item.contract_id
                for item in cast(
                    MultiLegStudyRequest,
                    self.inputs.multi_leg_request.payload,
                ).chain.contracts
            ),
        }
        if not actual_ids.issubset(set(self.spec.universe.instrument_ids)):
            raise BuiltinMultiAssetCodecError(
                "builtin_multi_asset_universe_coverage_mismatch"
            )
        object.__setattr__(
            self,
            "content_hash",
            _identity_hash(
                self.identity_payload(),
                label="builtin-multi-asset-request",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": _ARTIFACT_TYPE,
            "profile": self.profile.value,
            "run_id": self.run_id,
            "spec": self.spec.as_dict(),
            "evidence_references": [
                item.as_dict() for item in self.evidence_references
            ],
            "inputs": self.inputs.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> BuiltinMultiAssetRequest:
        payload = _mapping(value, "builtin_multi_asset_request")
        _reject_unsafe(payload, "builtin_multi_asset_request")
        _exact_fields(
            payload,
            {
                "schema_version",
                "artifact_type",
                "profile",
                "run_id",
                "spec",
                "evidence_references",
                "inputs",
                "content_hash",
            },
            "builtin_multi_asset_request",
        )
        if payload["artifact_type"] != _ARTIFACT_TYPE:
            raise BuiltinMultiAssetCodecError(
                "builtin_multi_asset_artifact_type_invalid"
            )
        raw_refs = payload["evidence_references"]
        if not isinstance(raw_refs, list):
            raise BuiltinMultiAssetCodecError(
                "builtin_multi_asset_evidence_array_required"
            )
        try:
            profile = BuiltinRunnerProfile(
                _text(payload["profile"], "builtin_multi_asset_request.profile")
            )
        except ValueError as exc:
            raise BuiltinMultiAssetCodecError(
                "builtin_multi_asset_profile_unsupported"
            ) from exc
        result = cls(
            run_id=_text(payload["run_id"], "builtin_multi_asset_request.run_id"),
            spec=multi_asset_experiment_spec_from_dict(
                _mapping(payload["spec"], "builtin_multi_asset_request.spec")
            ),
            evidence_references=tuple(
                EvidenceArtifactRef.from_dict(
                    _mapping(item, "builtin_multi_asset_request.evidence_ref")
                )
                for item in raw_refs
            ),
            inputs=BuiltinScenarioInputs.from_dict(payload["inputs"]),
            profile=profile,
            schema_version=_integer(
                payload["schema_version"],
                "builtin_multi_asset_request.schema_version",
            ),
        )
        expected_hash = _text(
            payload["content_hash"],
            "builtin_multi_asset_request.content_hash",
        )
        if result.content_hash != expected_hash:
            raise BuiltinMultiAssetCodecError(
                "builtin_multi_asset_request_content_hash_mismatch"
            )
        return result

    def to_application_request(
        self,
        *,
        paths: ResearchPathManager,
        command: Sequence[str],
        run_id: str | None = None,
    ) -> MultiAssetRunRequest:
        runners = _AuthoritativeBuiltinRunner(
            inputs=self.inputs,
        )
        return MultiAssetRunRequest(
            run_id=self.run_id if run_id is None else run_id,
            spec=self.spec,
            evidence_references=self.evidence_references,
            runners=MultiAssetScenarioRunners(
                spot=runners,
                futures=runners,
                option=runners,
                integrated=runners,
            ),
            paths=paths,
            command=tuple(command),
        )


def _position_payload(position: PositionView) -> dict[str, object]:
    return {
        "instrument_id": position.instrument_id,
        "asset_class": position.asset_class.value,
        "currency": position.currency,
        "quantity": _decimal_text(position.quantity),
        "average_price": _decimal_text(position.average_price),
        "mark_price": _decimal_text(position.mark_price),
        "multiplier": _decimal_text(position.multiplier),
    }


def _lifecycle_economic_payload(event: OptionLifecycleEvent) -> dict[str, object]:
    payload = event.identity_payload()
    payload.pop("position_id")
    payload.pop("source_position_hash")
    return payload


def _valuation_payload(snapshot: PortfolioSnapshot) -> dict[str, str]:
    valuation = snapshot.valuation(fx_rates={snapshot.base_currency: Decimal("1")})
    return {
        name: _decimal_text(getattr(valuation, name))
        for name in (
            "nav",
            "external_cash_flow",
            "economic_pnl",
            "realized_pnl",
            "unrealized_pnl",
            "income",
            "costs",
            "attributed_pnl",
        )
    }


def _scenario_accounting(
    snapshot: PortfolioSnapshot,
) -> ScenarioAccounting:
    valuation = snapshot.valuation(fx_rates={snapshot.base_currency: Decimal("1")})
    return ScenarioAccounting(
        opening_nav=Decimal("0"),
        external_cash_flow=valuation.external_cash_flow,
        closing_nav=valuation.nav,
        ledger_pnl=valuation.economic_pnl,
        report_pnl=valuation.attributed_pnl,
    )


def _scenario_objects(
    *,
    trades: Sequence[object],
    snapshot: PortfolioSnapshot,
    ledger_events: Sequence[object],
    exposure: object,
    attribution: object,
    scenario_output: object,
) -> ScenarioObjectHashes:
    return scenario_object_hashes(
        trades=trades,
        positions=tuple(_position_payload(item) for item in snapshot.positions),
        ledger_events=ledger_events,
        nav=(_valuation_payload(snapshot),),
        exposure=exposure,
        attribution=attribution,
        scenario_output=scenario_output,
    )


def _report_ledger_reconciliation(
    ledger: UnifiedPortfolioLedger,
    *,
    reconciliation_id: str,
) -> ReportLedgerReconciliation:
    snapshot = ledger.replay()
    if snapshot.as_of is None or not ledger.events:
        raise MultiAssetExperimentError("builtin_integrated_ledger_close_time_required")
    opening = UnifiedPortfolioLedger.open(
        ledger_id=ledger.ledger_id,
        base_currency=ledger.base_currency,
    )
    ledger_receipt = LedgerPnlReconciliation.from_ledger_projection(
        reconciliation_id=f"{reconciliation_id}.ledger",
        opening_ledger=opening,
        closing_ledger=ledger,
        opened_at=ledger.events[0].occurred_at,
        closed_at=snapshot.as_of,
        fx_observations=(),
    )
    report_payload = encode_report_payload(
        report_id=f"{reconciliation_id}.report",
        ledger=ledger_receipt,
    )
    report = ReportPnlSummary.from_json(
        report_payload,
        expected_payload_hash=report_payload_hash(report_payload),
    )
    return ReportLedgerReconciliation(
        reconciliation_id=reconciliation_id,
        ledger=ledger_receipt,
        report=report,
    )


def _evidence_source_hash(context: ScenarioRunContext, label: str) -> str:
    return evidence_hash(
        {
            "label": label,
            "experiment_spec_hash": context.spec.content_hash,
            "evidence_hashes": [
                item.reference.content_hash for item in context.evidence_artifacts
            ],
        },
        label="builtin-multi-asset-source",
    )


def _spot_exposure_authority(
    *,
    config: BuiltinSpotScenarioInput,
    snapshot: PortfolioSnapshot,
    source_hash: str,
) -> PortfolioExposureSnapshot:
    if snapshot.as_of is None or len(snapshot.spot_positions) != 1:
        raise MultiAssetExperimentError("builtin_spot_exposure_snapshot_invalid")
    position = snapshot.spot_positions[0]
    calendar_id = "builtin-offline-calendar"
    metadata = ObservationMetadata(
        observed_at=snapshot.as_of,
        knowledge_at=snapshot.as_of,
        source_hash=source_hash,
        calendar_id=calendar_id,
        max_age_seconds=0,
        quality=MarketDataQuality.GOOD,
    )
    market_state = MarketState(
        state_id=f"{config.instrument_id}.builtin.spot-state",
        valuation_at=snapshot.as_of,
        base_currency=config.currency,
        calendar_ids=(calendar_id,),
        spots=(
            SpotQuote(
                instrument_id=config.instrument_id,
                price=position.mark_price,
                currency=config.currency,
                unit=f"{config.currency}_per_unit",
                metadata=metadata,
            ),
        ),
    )
    decision = _timestamp(config.decision_at, "builtin_spot.decision_at")
    source = SourceReference(
        source_id="builtin-spot-request",
        source_version="v1",
        content_hash=source_hash,
        observed_at=config.knowledge_at,
    )
    period = EffectivePeriod(
        _timestamp_text(decision - timedelta(days=1)),
        None,
    )
    registry = InstrumentRegistry(
        economic_underlyings=(
            EconomicUnderlying(
                underlying_id=config.economic_underlying_id,
                name=(f"Builtin economic underlying {config.economic_underlying_id}"),
                asset_class="SPOT",
                unit="unit",
                currency=config.currency,
                validity=period,
                source=source,
            ),
        ),
        instruments=(
            Instrument(
                instrument_id=config.instrument_id,
                kind=InstrumentKind.SPOT,
                name=f"Builtin spot {config.instrument_id}",
                economic_underlying_id=config.economic_underlying_id,
                currency=config.currency,
                unit="unit",
                validity=period,
                source=source,
            ),
        ),
    )
    return ExposureEngine.with_default_spot(product_catalog=registry).evaluate(
        snapshot_id=f"{config.instrument_id}.builtin.spot-exposure",
        positions=(
            ExposurePosition(
                position_id=(f"{config.instrument_id}.builtin.spot-position"),
                instrument_id=config.instrument_id,
                quantity=position.quantity,
                quantity_unit="unit",
                multiplier=position.multiplier,
                currency=position.currency,
                source_hash=source_hash,
                opened_at=config.decision_at,
            ),
        ),
        market_state=market_state,
    )


@dataclass(slots=True)
class _AuthoritativeBuiltinRunner:
    """One source-owned runner implementing the four internal protocols."""

    inputs: BuiltinScenarioInputs
    runner_id: str = BUILTIN_RUNNER_ID
    runner_version: str = BUILTIN_RUNNER_VERSION
    _spot_ledgers: dict[int, UnifiedPortfolioLedger] = field(
        default_factory=dict,
        init=False,
    )
    _spot_traces: dict[int, SpotScenarioTrace] = field(
        default_factory=dict,
        init=False,
    )
    _spot_costs: dict[int, Decimal] = field(
        default_factory=dict,
        init=False,
    )
    _futures_steps: dict[int, tuple[SimulationStep, ...]] = field(
        default_factory=dict,
        init=False,
    )
    _option_positions: dict[int, OptionPosition] = field(
        default_factory=dict,
        init=False,
    )
    _option_selection_evidence: dict[
        int,
        tuple[tuple[str, ...], str, str],
    ] = field(
        default_factory=dict,
        init=False,
    )
    _resolved_inputs: dict[int, BuiltinScenarioInputs] = field(
        default_factory=dict,
        init=False,
    )
    _input_authority_receipts: dict[int, AuthoritativeInputReceipt] = field(
        default_factory=dict,
        init=False,
    )
    _spot_profile_receipts: dict[int, PublicSpotProfileReceipt] = field(
        default_factory=dict,
        init=False,
    )
    _futures_profile_receipts: dict[int, PublicFuturesProfileReceipt] = field(
        default_factory=dict,
        init=False,
    )
    _option_profile_receipts: dict[
        int,
        PublicOptionInstitutionalReceipt,
    ] = field(
        default_factory=dict,
        init=False,
    )
    _integrated_profile_receipts: dict[
        int,
        PublicIntegratedProfileReceipt,
    ] = field(
        default_factory=dict,
        init=False,
    )
    _profile_output_bindings: dict[
        int,
        dict[str, AuthoritativeOutputBinding],
    ] = field(
        default_factory=dict,
        init=False,
    )
    _execution_evidence_bundles: dict[
        int,
        PublicExecutionEvidenceBundle,
    ] = field(
        default_factory=dict,
        init=False,
    )

    def _authoritative_inputs(
        self,
        context: ScenarioRunContext,
    ) -> BuiltinScenarioInputs:
        """Resolve the immutable input document; the request copy is only a claim."""

        cached = self._resolved_inputs.get(context.repeat_index)
        if cached is not None:
            return cached
        try:
            receipt = AuthoritativeInputFactory(
                input_schema_id="builtin-multi-asset-scenario-inputs",
                input_schema_version=BUILTIN_MULTI_ASSET_SCHEMA_VERSION,
            ).resolve(
                context.artifacts_for(EvidenceArtifactRole.RESEARCH_INPUTS),
                input_document=self.inputs.as_dict(),
                decision_cutoff=context.spec.data_range.end_at,
            )
        except AuthoritativeInputError as exc:
            raise MultiAssetExperimentError(
                "builtin_research_input_authority_failed"
            ) from exc
        document = receipt.input_document
        try:
            authoritative = BuiltinScenarioInputs.from_dict(document)
        except BuiltinMultiAssetCodecError as exc:
            raise MultiAssetExperimentError(
                "builtin_research_inputs_document_invalid"
            ) from exc
        if (
            authoritative.content_hash != self.inputs.content_hash
            or authoritative != self.inputs
        ):
            raise MultiAssetExperimentError("builtin_request_input_claim_mismatch")
        self._resolved_inputs[context.repeat_index] = authoritative
        self._input_authority_receipts[context.repeat_index] = receipt
        return authoritative

    def _input_authority_receipt(
        self,
        context: ScenarioRunContext,
    ) -> AuthoritativeInputReceipt:
        self._authoritative_inputs(context)
        try:
            return self._input_authority_receipts[context.repeat_index]
        except KeyError as exc:  # pragma: no cover - method above establishes it.
            raise MultiAssetExperimentError(
                "builtin_research_input_authority_receipt_missing"
            ) from exc

    @staticmethod
    def _institutional_source_hashes(
        receipt: AuthoritativeInputReceipt,
    ) -> tuple[str, ...]:
        """Return canonical immutable document/row identities for profiles."""

        return tuple(
            sorted(
                {
                    receipt.artifact_hash,
                    receipt.input_document_hash,
                    receipt.source_rows_hash,
                    *receipt.source_row_hashes,
                }
            )
        )

    def _bind_profile_output(
        self,
        *,
        context: ScenarioRunContext,
        scenario_id: str,
        profile_receipt: object,
        profile_content_hash: str,
        input_paths: Sequence[str],
    ) -> AuthoritativeOutputBinding:
        input_receipt = self._input_authority_receipt(context)
        as_dict = getattr(profile_receipt, "as_dict", None)
        if not callable(as_dict):
            raise MultiAssetExperimentError(
                "builtin_institutional_profile_receipt_invalid"
            )
        binding = input_receipt.bind_output(
            output_path=f"/{scenario_id}/institutional_receipt",
            output_value=as_dict(),
            input_paths=input_paths,
            computation_hash=profile_content_hash,
        )
        input_receipt.source_rows_for_output(binding)
        self._profile_output_bindings.setdefault(
            context.repeat_index,
            {},
        )[scenario_id] = binding
        return binding

    def option_selection_evidence(
        self,
        repeat_index: int,
    ) -> tuple[tuple[str, ...], str, str]:
        try:
            return self._option_selection_evidence[repeat_index]
        except KeyError as exc:
            raise MultiAssetExperimentError(
                "builtin_option_selection_evidence_missing"
            ) from exc

    def public_execution_evidence(
        self,
        repeat_index: int,
    ) -> PublicExecutionEvidenceBundle:
        try:
            return self._execution_evidence_bundles[repeat_index]
        except KeyError as exc:
            raise MultiAssetExperimentError(
                "builtin_public_execution_evidence_missing"
            ) from exc

    def run_spot(self, context: ScenarioRunContext) -> SpotScenarioTrace:
        inputs = self._authoritative_inputs(context)
        input_receipt = self._input_authority_receipt(context)
        config = inputs.spot
        policy = inputs.policy
        decision = _timestamp(config.decision_at, "builtin_spot.decision_at")
        knowledge = _timestamp(config.knowledge_at, "builtin_spot.knowledge_at")
        membership = UniverseMembership(
            universe_id=f"{context.spec.experiment_id}.spot-universe",
            instrument_id=config.instrument_id,
            effective_from=decision - timedelta(days=1),
            effective_to=None,
            announcement_at=decision - timedelta(days=2),
            implementation_at=decision - timedelta(days=1),
            known_at=knowledge,
            membership_source_hash=_evidence_source_hash(
                context,
                "spot-universe-membership",
            ),
        )
        members = PointInTimeSpotUniverse((membership,)).members(
            membership.universe_id,
            effective_at=decision,
            knowledge_at=decision,
        )
        if members != (config.instrument_id,):
            raise MultiAssetExperimentError("builtin_spot_universe_selection_failed")
        hypothesis = EconomicHypothesis(
            hypothesis_id=context.spec.hypothesis.logical_id,
            version=context.spec.hypothesis.version,
            economic_underlying_id=config.economic_underlying_id,
            rationale=context.spec.hypothesis.rationale,
            expected_direction=Direction.LONG,
            distribution=ExpectedMarketDistribution(
                expected_return=policy.expected_return,
                annualized_volatility=policy.annualized_volatility,
                downside_tail_return=policy.downside_tail_return,
                upside_return=policy.upside_return,
                horizon_days=policy.horizon_days,
                risk_free_rate=Decimal("0"),
                dividend_yield=Decimal("0"),
                volatility_change=Decimal("0"),
                liquidity_change=Decimal("0"),
                scenarios=(
                    ScenarioRange(
                        name="base",
                        probability=Decimal("1"),
                        lower_return=policy.downside_tail_return,
                        upper_return=policy.upside_return,
                    ),
                ),
            ),
            conditions=("point_in_time_inputs_available",),
            failure_conditions=("non_positive_net_return",),
            prediction_target=context.spec.hypothesis.statement,
            evaluation_metrics=tuple(
                item.logical_id for item in context.spec.evaluation_metrics
            ),
        )
        target_notional = config.quantity * config.entry_price
        expected_transaction_cost = config.quantity * config.commission_per_unit
        payoff = DesiredEconomicPayoff(
            underlying_id=config.economic_underlying_id,
            direction=Direction.LONG,
            horizon_days=policy.horizon_days,
            target_notional=target_notional,
            target_delta=None,
            target_vega=None,
            target_volatility=None,
            maximum_loss=target_notional,
            maximum_premium=None,
            tail_protection_required=False,
            bounded_loss_required=False,
            allowed_expression_kinds=(ExpressionKind.SPOT,),
        )
        choice = InstrumentChoice(
            instrument_id=config.instrument_id,
            economic_underlying_id=config.economic_underlying_id,
            product_kind=ProductKind.SPOT,
            currency=config.currency,
            known_at=knowledge,
            unit_price=config.entry_price,
            contract_multiplier=Decimal("1"),
            economic_notional_per_unit=config.entry_price,
            liquidity_score=Decimal("1"),
            expected_return=policy.expected_return,
            expected_carry=Decimal("0"),
            expected_roll_cost=Decimal("0"),
            expected_time_value_decay=Decimal("0"),
            implied_volatility=None,
            transaction_cost=config.commission_per_unit,
            initial_margin=Decimal("0"),
            tail_loss=target_notional,
            model_sensitivity=Decimal("0"),
            data_confidence=Decimal("1"),
        )
        candidate = ExpressionCandidate(
            candidate_id=f"{context.spec.experiment_id}.spot-expression",
            expression_kind=ExpressionKind.SPOT,
            choices=(choice,),
            directions=(Direction.LONG,),
            roles=(LegRole.PRIMARY,),
            leg_ratios=(Decimal("1"),),
            selection_rules=(LegSelectionRule(product_kind=ProductKind.SPOT),),
            execution_mode=ExecutionMode.SIMULTANEOUS_ATOMIC,
            expected_return=policy.expected_return,
            pnl_dispersion=policy.annualized_volatility,
            maximum_loss=target_notional,
            carry=Decimal("0"),
            roll_cost=Decimal("0"),
            time_value_decay=Decimal("0"),
            implied_volatility_cost=Decimal("0"),
            liquidity_score=Decimal("1"),
            transaction_cost=expected_transaction_cost,
            margin_required=Decimal("0"),
            tail_risk=abs(policy.downside_tail_return),
            model_sensitivity=Decimal("0"),
            data_confidence=Decimal("1"),
            targets=StrategyTargets(target_notional=target_notional),
        )
        expression_engine = InstrumentExpressionEngine(DEFAULT_EXPRESSION_POLICY)
        generated_groups = expression_engine.generate_candidate_groups(
            payoff=payoff,
            instruments=(choice,),
            as_of=decision,
        )
        if generated_groups != ((ExpressionKind.SPOT, (choice,)),):
            raise MultiAssetExperimentError("builtin_spot_expression_generation_failed")
        expression = expression_engine.select(
            hypothesis=hypothesis,
            payoff=payoff,
            candidates=(candidate,),
            as_of=decision,
        )
        if (
            expression.selected_candidate_id != candidate.candidate_id
            or expression.selected_legs[0].quantity != config.quantity
        ):
            raise MultiAssetExperimentError("builtin_spot_expression_selection_failed")

        funding = target_notional * Decimal("2")
        ledger = UnifiedPortfolioLedger.open(
            ledger_id=f"{context.spec.experiment_id}.builtin.ledger",
            base_currency=config.currency,
        ).publish(
            funding_event(
                event_id=f"{context.spec.experiment_id}.builtin.funding",
                occurred_at=_timestamp_text(decision - timedelta(minutes=1)),
                cash_deltas=(CashDelta(config.currency, funding),),
            )
        )
        trade = trade_event(
            event_id=f"{context.spec.experiment_id}.builtin.spot-entry",
            occurred_at=_timestamp_text(decision),
            asset_class=AssetClass.SPOT,
            instrument_id=config.instrument_id,
            currency=config.currency,
            quantity_delta=config.quantity,
            price=config.entry_price,
            source_hashes=(expression.content_hash,),
            execution_context_hash=expression.content_hash,
        )
        execution_context = ExecutionContext(
            execution_id=(f"{context.spec.experiment_id}.builtin.spot-execution"),
            instrument_id=config.instrument_id,
            instrument_kind="SPOT",
            currency=config.currency,
            side=ExecutionSide.BUY,
            requested_quantity=config.quantity,
            filled_quantity=config.quantity,
            reference_price=config.entry_price,
            execution_price=config.entry_price,
            observed_at=config.decision_at,
            capacity_quantity=config.quantity * Decimal("100"),
            participation_rate=Decimal("0.01"),
            source_hashes=(expression.content_hash,),
        )
        spot_cost = LinearExecutionCostModel(
            commission_per_unit=config.commission_per_unit
        ).estimate(execution_context)
        ledger = ledger.publish(trade).publish_many(
            cost_events_from_breakdown(
                spot_cost,
                event_id_prefix=(
                    f"{context.spec.experiment_id}.builtin.spot-entry-cost"
                ),
                occurred_at=config.decision_at,
                instrument_id=config.instrument_id,
                asset_class=AssetClass.SPOT,
                source_hashes=(expression.content_hash,),
            )
        )
        spot_trade_hash = next(
            item.content_hash
            for item in ledger.events
            if item.event_id == trade.event_id
        )
        entry_book = SpotBook(
            positions=(
                SpotPosition(
                    instrument_id=config.instrument_id,
                    quantity=config.quantity,
                    total_cost_basis=target_notional,
                    currency=config.currency,
                ),
            ),
            cash=(
                SpotCashBalance(
                    config.currency,
                    funding - target_notional,
                ),
            ),
        )
        split = CorporateAction(
            action_id=f"{context.spec.experiment_id}.builtin.split",
            revision=1,
            action_type=CorporateActionType.SPLIT,
            instrument_id=config.instrument_id,
            announced_at=decision - timedelta(days=1),
            known_at=knowledge,
            record_at=None,
            ex_at=None,
            payment_at=None,
            effective_at=decision + timedelta(days=1),
            source_id="externally-prepared-corporate-actions",
            source_record_hash=_evidence_source_hash(context, "spot-split"),
            ratio=config.split_ratio,
        )
        split_application = apply_corporate_action(
            entry_book,
            split,
            applied_at=split.effective_at,
        )
        split_mark = config.entry_price / config.split_ratio
        ledger = ledger.publish_many(
            adapt_corporate_action_application(
                split_application,
                mark_prices_after={config.instrument_id: split_mark},
            )
        )
        dividend = CorporateAction(
            action_id=f"{context.spec.experiment_id}.builtin.dividend",
            revision=1,
            action_type=CorporateActionType.CASH_DIVIDEND,
            instrument_id=config.instrument_id,
            announced_at=decision - timedelta(days=1),
            known_at=knowledge,
            record_at=decision + timedelta(days=1),
            ex_at=decision + timedelta(days=1),
            payment_at=decision + timedelta(days=2),
            effective_at=decision + timedelta(days=2),
            source_id="externally-prepared-corporate-actions",
            source_record_hash=_evidence_source_hash(context, "spot-dividend"),
            currency=config.currency,
            cash_per_share=config.dividend_per_unit,
            tax_rate=config.dividend_tax_rate,
        )
        dividend_application = apply_corporate_action(
            split_application.book_after,
            dividend,
            applied_at=dividend.effective_at,
            entitlement_book=split_application.book_after,
        )
        dividend_drafts = adapt_corporate_action_application(dividend_application)
        ledger = ledger.publish_many(dividend_drafts)
        snapshot = ledger.replay()
        valuation = snapshot.valuation(fx_rates={config.currency: Decimal("1")})
        spot_exposure = _spot_exposure_authority(
            config=config,
            snapshot=snapshot,
            source_hash=ledger.content_hash,
        )
        before_value = split_application.book_before.value(
            prices={config.instrument_id: config.entry_price},
            fx_to_base={config.currency: Decimal("1")},
        )
        after_value = split_application.book_after.value(
            prices={config.instrument_id: split_mark},
            fx_to_base={config.currency: Decimal("1")},
        )
        portfolio_cash = dividend_application.book_after.cash_amount(
            config.currency
        ) - dividend_application.book_before.cash_amount(config.currency)
        ledger_cash = sum(
            (delta.amount for draft in dividend_drafts for delta in draft.cash_deltas),
            Decimal("0"),
        )
        institutional_receipt = run_public_t01_spot_profile(
            build_public_t01_inputs(
                source_document_id=input_receipt.artifact_logical_id,
                source_document_hashes=self._institutional_source_hashes(input_receipt),
                observation_at=_timestamp_text(decision - timedelta(days=10)),
                valuation_at=config.decision_at,
                knowledge_at=config.knowledge_at,
                instrument_id=config.instrument_id,
                currency=config.currency,
                entry_price=config.entry_price,
                quantity=config.quantity,
            )
        )
        if institutional_receipt.resolved_instrument_id != config.instrument_id:
            raise MultiAssetExperimentError(
                "builtin_spot_institutional_profile_identity_mismatch"
            )
        institutional_binding = self._bind_profile_output(
            context=context,
            scenario_id="T-01",
            profile_receipt=institutional_receipt,
            profile_content_hash=institutional_receipt.content_hash,
            input_paths=("/spot",),
        )
        self._spot_profile_receipts[context.repeat_index] = institutional_receipt
        objects = _scenario_objects(
            trades=(expression,),
            snapshot=snapshot,
            ledger_events=tuple(item.as_dict() for item in ledger.events),
            exposure=spot_exposure.as_dict(),
            attribution={
                "economic_pnl": _decimal_text(valuation.economic_pnl),
                "costs": _decimal_text(valuation.costs),
                "cost_breakdown_hash": spot_cost.content_hash,
                "gross_performance": _decimal_text(
                    valuation.economic_pnl + valuation.costs
                ),
                "net_performance": _decimal_text(valuation.economic_pnl),
            },
            scenario_output={
                "expression_hash": expression.content_hash,
                "split_hash": split_application.book_after_hash,
                "dividend_hash": dividend_application.book_after_hash,
                "execution_cost_hash": spot_cost.content_hash,
                "input_authority_receipt_hash": input_receipt.content_hash,
                "institutional_profile_receipt": (institutional_receipt.as_dict()),
                "institutional_output_binding": (institutional_binding.as_dict()),
            },
        )
        data_hashes = tuple(
            sorted(
                item.reference.content_hash
                for item in context.artifacts_for(EvidenceArtifactRole.DATASET)
            )
        )
        code_hash = context.one_artifact(
            EvidenceArtifactRole.CODE
        ).reference.content_hash
        trace = SpotScenarioTrace(
            decision_at=config.decision_at,
            maximum_universe_knowledge_at=config.knowledge_at,
            universe_snapshot_hash=evidence_hash(
                {
                    "membership": membership.membership_source_hash,
                    "members": members,
                },
                label="builtin-spot-universe",
            ),
            signal_hash=expression.content_hash,
            selected_instrument_ids=members,
            trade_hashes=(spot_trade_hash,),
            position_hash=evidence_hash(
                _position_payload(snapshot.spot_positions[0]),
                label="builtin-spot-position",
            ),
            ledger_hash=ledger.content_hash,
            nav_hash=objects.nav_hash,
            exposure_hash=spot_exposure.content_hash,
            artifact_hash=evidence_hash(
                {
                    "ledger_hash": ledger.content_hash,
                    "exposure_hash": spot_exposure.content_hash,
                    "objects": objects.as_dict(),
                },
                label="builtin-spot-artifact",
            ),
            corporate_action_value_before=before_value,
            corporate_action_value_after=after_value,
            portfolio_cashflow=portfolio_cash,
            ledger_cashflow=ledger_cash,
            gross_performance=valuation.economic_pnl + valuation.costs,
            net_performance=valuation.economic_pnl,
            data_version_hashes=data_hashes,
            code_hash=code_hash,
            accounting=_scenario_accounting(snapshot),
            object_hashes=objects,
            quality_flags=(
                "AUTHORITATIVE_CORPORATE_ACTION_ENGINE",
                "AUTHORITATIVE_EXPRESSION_ENGINE",
                "IMMUTABLE_SOURCE_ROW_INPUT_AUTHORITY",
                "POINT_IN_TIME_UNIVERSE",
                "PUBLIC_T01_INSTITUTIONAL_PROFILE",
            ),
        )
        self._spot_ledgers[context.repeat_index] = ledger
        self._spot_traces[context.repeat_index] = trace
        self._spot_costs[context.repeat_index] = spot_cost.total
        return trace

    def run_futures(
        self,
        context: ScenarioRunContext,
    ) -> FuturesScenarioTrace:
        inputs = self._authoritative_inputs(context)
        input_receipt = self._input_authority_receipt(context)
        request = cast(
            FuturesStudyRequest,
            inputs.futures_request.payload,
        )
        entry_commands = tuple(
            item for item in request.commands if isinstance(item, FuturesOrderCommand)
        )
        roll_commands = tuple(
            item for item in request.commands if isinstance(item, FuturesRollCommand)
        )
        if len(entry_commands) != 1 or len(roll_commands) != 1:
            raise MultiAssetExperimentError(
                "builtin_futures_single_entry_and_roll_required"
            )
        entry_command = entry_commands[0]
        roll_command = roll_commands[0]
        continuous_trace = trace_continuous_signal(
            inputs.futures_signal_points,
            trace_id=(f"{context.spec.experiment_id}.builtin.continuous.trace"),
        )
        entry_points = tuple(
            item
            for item in inputs.futures_signal_points
            if item.content_hash == entry_command.intent.signal_point_hash
            and item.source_contract_id == entry_command.intent.contract_id
        )
        roll_points = tuple(
            item
            for item in inputs.futures_signal_points
            if item.roll_decision_hash == roll_command.decision.content_hash
        )
        if (
            len(entry_points) != 1
            or len(roll_points) != 1
            or roll_points[0].source_contract_id != roll_command.decision.to_contract_id
            or roll_points[0].observed_at != roll_command.decision.decision_at
            or roll_points[0].policy_hash != roll_command.decision.policy_hash
        ):
            raise MultiAssetExperimentError(
                "builtin_futures_signal_command_binding_mismatch"
            )
        entry_point = entry_points[0]
        if _timestamp(
            entry_point.observed_at,
            "builtin_futures.entry_signal_observed_at",
        ) > _timestamp(
            entry_command.intent.decision_at,
            "builtin_futures.entry_decision_at",
        ):
            raise MultiAssetExperimentError("builtin_futures_entry_signal_from_future")
        entry_point_index = inputs.futures_signal_points.index(entry_point)
        if entry_point_index == 0:
            raise MultiAssetExperimentError("builtin_futures_signal_history_required")
        previous_signal_point = inputs.futures_signal_points[entry_point_index - 1]
        signal_return = (
            entry_point.continuous_price / previous_signal_point.continuous_price
            - Decimal("1")
        )
        threshold = inputs.policy.futures_signal_return_threshold
        if abs(signal_return) <= threshold:
            raise MultiAssetExperimentError(
                "builtin_futures_signal_below_entry_threshold"
            )
        signal_side = (
            FuturesOrderSide.BUY if signal_return > threshold else FuturesOrderSide.SELL
        )
        entry_contract = request.simulator.contract_for(entry_point.source_contract_id)
        entry_signal_quote = request.chain.quote_for(
            entry_point.source_contract_id,
            entry_command.intent.decision_at,
        )
        contract_notional = (
            entry_signal_quote.close_price * entry_contract.contract_multiplier
        )
        raw_contract_quantity = (
            inputs.policy.futures_target_notional / contract_notional
        )
        expected_contract_quantity = int(
            raw_contract_quantity.to_integral_value(rounding=ROUND_FLOOR)
        )
        if expected_contract_quantity <= 0:
            raise MultiAssetExperimentError(
                "builtin_futures_target_notional_below_one_contract"
            )
        entry_sizing_payload = {
            "target_notional": _decimal_text(inputs.policy.futures_target_notional),
            "contract_price": _decimal_text(entry_signal_quote.close_price),
            "contract_multiplier": _decimal_text(entry_contract.contract_multiplier),
            "raw_contract_quantity": _decimal_text(raw_contract_quantity),
            "rounded_contract_quantity": expected_contract_quantity,
            "rounding": "ROUND_FLOOR",
        }
        if (
            entry_command.intent.side is not signal_side
            or entry_command.intent.quantity != expected_contract_quantity
            or entry_command.intent.signal_series_id != continuous_trace.series_id
            or entry_command.intent.signal_point_hash != entry_point.content_hash
        ):
            raise MultiAssetExperimentError(
                "builtin_futures_order_not_derived_from_signal"
            )
        roll_point_index = inputs.futures_signal_points.index(roll_points[0])
        if (
            roll_point_index == 0
            or inputs.futures_signal_points[roll_point_index - 1].source_contract_id
            != roll_command.decision.from_contract_id
        ):
            raise MultiAssetExperimentError(
                "builtin_futures_signal_roll_source_mismatch"
            )
        signal_quotes = tuple(
            request.chain.quote_for(
                point.source_contract_id,
                point.observed_at,
            )
            for point in inputs.futures_signal_points
        )
        if any(
            point.source_quote_hash != quote.content_hash
            or point.source_price != quote.close_price
            for point, quote in zip(
                inputs.futures_signal_points,
                signal_quotes,
                strict=True,
            )
        ):
            raise MultiAssetExperimentError("builtin_futures_signal_pit_quote_mismatch")
        for contract_id in (
            entry_command.intent.contract_id,
            roll_command.decision.from_contract_id,
            roll_command.decision.to_contract_id,
        ):
            continuous_trace.require_executable_contract(contract_id)
        execution = DerivativeResearchApplicationService().run_futures(request)
        ledger = FuturesLedger.open(request.ledger_id, request.initial_cash)
        steps: list[SimulationStep] = []
        for command in request.commands:
            if isinstance(command, FuturesOrderCommand):
                quote = request.chain.quote_for(
                    command.intent.contract_id,
                    command.intent.decision_at,
                )
                step = request.simulator.execute(
                    ledger,
                    command.intent,
                    quote,
                    fill_id=command.fill_id,
                    step_id=command.step_id,
                )
            elif isinstance(command, FuturesSettlementCommand):
                quote = request.chain.quote_for(
                    command.contract_id,
                    command.as_of,
                )
                step = request.simulator.settle_daily(
                    ledger,
                    quote,
                    event_id=command.event_id,
                    step_id=command.step_id,
                    as_of=command.as_of,
                )
            elif isinstance(command, FuturesRollCommand):
                old_quote = request.chain.quote_for(
                    command.decision.from_contract_id,
                    command.decision.decision_at,
                )
                new_quote = request.chain.quote_for(
                    command.decision.to_contract_id,
                    command.decision.decision_at,
                )
                step = request.simulator.roll(
                    ledger,
                    command.decision,
                    old_quote,
                    new_quote,
                    execution_id=command.execution_id,
                    step_id=command.step_id,
                )
            else:
                raise MultiAssetExperimentError("builtin_futures_command_unsupported")
            ledger = step.ledger
            steps.append(step)
        payload = execution.simulation.simulation_payload
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or [item.content_hash for item in steps] != [
            _text(
                _mapping(item, "builtin_futures.step")["content_hash"],
                "builtin_futures.step.content_hash",
            )
            for item in raw_steps
        ]:
            raise MultiAssetExperimentError(
                "builtin_futures_application_object_mismatch"
            )
        roll_steps = [item for item in steps if item.roll_execution is not None]
        settlement_events = tuple(
            event for item in steps for event in item.settlement_events
        )
        entry_fills = tuple(
            fill for item in steps for fill in item.fills if item.roll_execution is None
        )
        if (
            len(roll_steps) != 1
            or not settlement_events
            or not entry_fills
            or len(roll_steps[0].fills) != 2
        ):
            raise MultiAssetExperimentError("builtin_futures_open_settle_roll_required")
        roll_step = roll_steps[0]
        roll = roll_step.roll_execution
        if roll is None:  # pragma: no cover - narrowed above
            raise MultiAssetExperimentError("builtin_futures_roll_execution_required")
        close_fill, open_fill = roll_step.fills
        executed_ids = tuple(
            sorted({fill.contract_id for item in steps for fill in item.fills})
        )
        source_mappings = tuple(
            FuturesSourceMapping(
                trading_date=quote.trading_date,
                continuous_point_hash=mapping.point_hash,
                source_contract_id=mapping.source_contract_id,
            )
            for mapping, quote in zip(
                continuous_trace.mappings,
                signal_quotes,
                strict=True,
            )
        )
        if {item.source_contract_id for item in source_mappings} != set(executed_ids):
            raise MultiAssetExperimentError(
                "builtin_futures_continuous_mapping_coverage_mismatch"
            )
        old_contract = next(
            item
            for item in request.simulator.contracts
            if item.contract_id == roll.from_contract_id
        )
        last_notice = old_contract.first_notice_date or old_contract.last_trade_date
        last_notice_at = f"{last_notice}T00:00:00Z"
        last_trade_at = f"{old_contract.last_trade_date}T00:00:00Z"
        final_action_at = roll.executed_at
        final_cash = ledger.cash_balance
        pnl = final_cash - request.initial_cash
        selected_contract = request.simulator.contract_for(
            roll_command.decision.to_contract_id
        )
        selected_quote = request.chain.quote_for(
            selected_contract.contract_id,
            roll_command.decision.decision_at,
        )
        institutional_position_quantity = Decimal(open_fill.quantity) * (
            Decimal("1") if open_fill.side is FuturesOrderSide.BUY else Decimal("-1")
        )
        institutional_receipt = run_public_t02_futures_profile(
            build_public_t02_inputs(
                source_document_id=input_receipt.artifact_logical_id,
                source_document_hashes=self._institutional_source_hashes(input_receipt),
                observation_at=entry_point.observed_at,
                valuation_at=roll_command.decision.decision_at,
                knowledge_at=request.chain.availability.processed_at,
                underlying_instrument_id=inputs.spot.instrument_id,
                root_id=request.chain.root_id,
                near_contract_id=entry_command.intent.contract_id,
                selected_contract_id=selected_contract.contract_id,
                currency=inputs.spot.currency,
                entry_price=open_fill.fill_price,
                final_price=selected_quote.settlement_price,
                multiplier=open_fill.multiplier,
                quantity=institutional_position_quantity,
                settlement_mode=(
                    FuturesSettlementMode.PHYSICAL
                    if selected_contract.settlement_type.value == "PHYSICAL_SETTLED"
                    else FuturesSettlementMode.CASH
                ),
            )
        )
        if (
            institutional_receipt.selected_contract_id
            != roll_command.decision.to_contract_id
            or institutional_receipt.selected_contract_multiplier
            != open_fill.multiplier
            or institutional_receipt.position_quantity
            != institutional_position_quantity
            or institutional_receipt.prior_settlement_price != open_fill.fill_price
        ):
            raise MultiAssetExperimentError(
                "builtin_futures_institutional_profile_economics_mismatch"
            )
        institutional_binding = self._bind_profile_output(
            context=context,
            scenario_id="T-02",
            profile_receipt=institutional_receipt,
            profile_content_hash=institutional_receipt.content_hash,
            input_paths=(
                "/futures_request",
                "/futures_signal_points",
                "/policy",
                "/spot",
            ),
        )
        self._futures_profile_receipts[context.repeat_index] = institutional_receipt
        objects = scenario_object_hashes(
            trades=tuple(fill.as_dict() for item in steps for fill in item.fills),
            positions=tuple(item.as_dict() for item in ledger.positions),
            ledger_events=tuple(
                {
                    "step_hash": item.content_hash,
                    "ledger_hash": item.ledger.content_hash,
                    "event_hashes": list(item.ledger.event_hashes),
                }
                for item in steps
            ),
            nav=(
                {
                    "initial_cash": _decimal_text(request.initial_cash),
                    "closing_cash": _decimal_text(final_cash),
                    "pnl": _decimal_text(pnl),
                },
            ),
            exposure=(
                {
                    "roll_quantity_plan": (roll_step.roll_quantity_plan.as_dict()),
                    "continuous_signal_trace_hash": (continuous_trace.content_hash),
                    "entry_sizing": entry_sizing_payload,
                }
                if roll_step.roll_quantity_plan is not None
                else {"roll_execution_hash": roll.content_hash}
            ),
            attribution={
                "variation_margin": _decimal_text(
                    sum(
                        (item.variation_margin for item in settlement_events),
                        Decimal("0"),
                    )
                ),
                "cumulative_fees": _decimal_text(ledger.cumulative_fees),
                "native_ledger_pnl": _decimal_text(pnl),
            },
            scenario_output={
                "derivative_simulation": execution.simulation.as_dict(),
                "input_authority_receipt_hash": input_receipt.content_hash,
                "institutional_profile_receipt": (institutional_receipt.as_dict()),
                "institutional_output_binding": (institutional_binding.as_dict()),
            },
        )
        self._futures_steps[context.repeat_index] = tuple(steps)
        return FuturesScenarioTrace(
            continuous_series_id=continuous_trace.series_id,
            source_mappings=source_mappings,
            executed_contract_ids=executed_ids,
            entry_fill_hashes=tuple(item.content_hash for item in entry_fills),
            settlement_hashes=tuple(item.content_hash for item in settlement_events),
            roll_close_fill_hash=close_fill.content_hash,
            roll_open_fill_hash=open_fill.content_hash,
            roll_ledger_event_hashes=(
                close_fill.content_hash,
                open_fill.content_hash,
            ),
            last_notice_at=last_notice_at,
            last_trade_at=last_trade_at,
            final_action_at=final_action_at,
            settlement_pnl=pnl,
            ledger_pnl=pnl,
            accounting=ScenarioAccounting(
                opening_nav=Decimal("0"),
                external_cash_flow=request.initial_cash,
                closing_nav=final_cash,
                ledger_pnl=pnl,
                report_pnl=pnl,
            ),
            object_hashes=objects,
            quality_flags=(
                "AUTHORITATIVE_DERIVATIVE_APPLICATION",
                "AUTHORITATIVE_FUTURES_SIMULATOR",
                "CONTRACT_MULTIPLIER_SIZING",
                "EXPOSURE_PRESERVING_ROLL",
                "IMMUTABLE_SOURCE_ROW_INPUT_AUTHORITY",
                "PUBLIC_T02_INSTITUTIONAL_PROFILE",
            ),
        )

    def run_option(
        self,
        context: ScenarioRunContext,
    ) -> OptionScenarioTrace:
        inputs = self._authoritative_inputs(context)
        input_receipt = self._input_authority_receipt(context)
        request = cast(
            OptionStudyRequest,
            inputs.option_request.payload,
        )
        if len(request.orders) != 1 or request.orders[0].lifecycle is None:
            raise MultiAssetExperimentError(
                "builtin_option_single_order_lifecycle_required"
            )
        command = request.orders[0]
        contract = request.chain.contract(command.contract_id)
        quote = request.chain.quote(command.contract_id)
        decision_at = _timestamp(
            command.requested_at,
            "builtin_option.decision_at",
        )
        candidate_inputs: dict[str, ValuationInputSnapshot] = {}
        candidate_forwards: dict[str, ForwardEstimate] = {}
        candidate_implied: dict[str, Decimal] = {}
        candidate_greeks: dict[str, OptionGreeks] = {}
        raw_observations: list[RawOptionObservation] = []
        calculated_deltas: list[CalculatedOptionDelta] = []
        for candidate_contract in sorted(
            request.chain.contracts,
            key=lambda item: item.contract_id,
        ):
            candidate_quote = request.chain.quote(candidate_contract.contract_id)
            if (
                _timestamp(
                    candidate_quote.as_of,
                    "builtin_option.candidate_quote_as_of",
                )
                != decision_at
            ):
                raise MultiAssetExperimentError(
                    "builtin_option_chain_not_aligned_to_decision"
                )
            if candidate_contract.contract_id == command.contract_id:
                valuation_input = command.valuation_input
            else:
                time_years = Decimal(
                    str(
                        (
                            _timestamp(
                                candidate_contract.expiration_at,
                                "builtin_option.candidate_expiration",
                            )
                            - decision_at
                        ).total_seconds()
                        / (365 * 24 * 60 * 60)
                    )
                )
                if time_years < 0:
                    raise MultiAssetExperimentError(
                        "builtin_option_chain_contains_expired_contract"
                    )
                forward_value = command.valuation_input.spot_price * Decimal(
                    str(
                        math.exp(
                            float(
                                (
                                    command.valuation_input.risk_free_rate
                                    - command.valuation_input.dividend_yield
                                )
                                * time_years
                            )
                        )
                    )
                )
                valuation_input = replace(
                    command.valuation_input,
                    valuation_input_id=(
                        f"{command.valuation_input.valuation_input_id}."
                        f"candidate.{candidate_contract.contract_id}"
                    ),
                    contract=candidate_contract,
                    quote=candidate_quote,
                    valuation_at=command.requested_at,
                    forward_price=forward_value,
                )
            implied_result = request.valuation_model.implied_volatility(
                valuation_input,
                permit_illiquid=request.execution_policy.allow_illiquid,
            )
            if not implied_result.success or implied_result.volatility is None:
                raise MultiAssetExperimentError(
                    "builtin_option_candidate_implied_volatility_required"
                )
            candidate_greek = request.valuation_model.greeks(
                valuation_input,
                implied_result.volatility,
            )
            candidate_forward = ForwardEstimate(
                value=valuation_input.forward_price,
                method=ForwardMethod.SPOT_CARRY,
                estimated_at=decision_at,
                input_hashes=valuation_input.source_manifest_hashes,
                rate=valuation_input.risk_free_rate,
                dividend_yield=valuation_input.dividend_yield,
                borrow_rate=Decimal("0"),
            )
            candidate_inputs[candidate_contract.contract_id] = valuation_input
            candidate_forwards[candidate_contract.contract_id] = candidate_forward
            candidate_implied[candidate_contract.contract_id] = (
                implied_result.volatility
            )
            candidate_greeks[candidate_contract.contract_id] = candidate_greek
            raw_observations.append(
                RawOptionObservation(
                    contract_id=candidate_contract.contract_id,
                    underlying_id=candidate_contract.underlying_id,
                    right=PathOptionRight(candidate_contract.option_type.value),
                    strike=candidate_contract.strike,
                    expiry=_timestamp(
                        candidate_contract.expiration_at,
                        "builtin_option.candidate_expiration",
                    ),
                    observed_at=_timestamp(
                        candidate_quote.as_of,
                        "builtin_option.candidate_quote_as_of",
                    ),
                    known_at=_timestamp(
                        request.chain.knowledge_time,
                        "builtin_option.chain_knowledge_time",
                    ),
                    bid=candidate_quote.bid,
                    ask=candidate_quote.ask,
                    bid_size=candidate_quote.bid_size,
                    ask_size=candidate_quote.ask_size,
                    volume=candidate_quote.volume,
                    open_interest=candidate_quote.open_interest,
                    bid_iv=implied_result.volatility,
                    ask_iv=implied_result.volatility,
                    delta=candidate_greek.delta,
                    source_quote_hash=candidate_quote.content_hash,
                    adjusted_contract=candidate_contract.adjusted_contract,
                )
            )
            calculated_deltas.append(
                CalculatedOptionDelta(
                    contract_id=candidate_contract.contract_id,
                    calculated_at=decision_at,
                    known_at=decision_at,
                    delta=candidate_greek.delta,
                    market_state_hash=request.chain.content_hash,
                    model_specification_hash=(request.valuation_model.content_hash),
                    valuation_input_hash=valuation_input.content_hash,
                    source_quote_hash=candidate_quote.content_hash,
                    forward_hash=candidate_forward.content_hash,
                )
            )
        implied = candidate_implied[command.contract_id]
        greeks = candidate_greeks[command.contract_id]
        forward = candidate_forwards[command.contract_id]
        cleaned_chain = OptionChainCleaner(
            OptionCleaningPolicy(
                policy_id="builtin-option-chain-cleaning",
                version="v1",
                maximum_age_seconds=300,
                maximum_relative_spread=Decimal("1"),
                minimum_quote_size=Decimal("1"),
                minimum_volume=0,
                minimum_open_interest=0,
                minimum_iv=Decimal("0.000001"),
                maximum_iv=Decimal("10"),
                reject_adjusted_contracts=True,
            )
        ).clean(
            underlying_id=contract.underlying_id,
            decision_at=decision_at,
            market_state_hash=request.chain.content_hash,
            spot=command.valuation_input.spot_price,
            forward=forward,
            observations=tuple(raw_observations),
        )
        selection = select_option_contract(
            cleaned_chain,
            OptionSelectionPolicy(
                policy_id="builtin-option-selection",
                version="v1",
                right=PathOptionRight(contract.option_type.value),
                target_days_to_expiry=(inputs.policy.option_target_days_to_expiry),
                minimum_days_to_expiry=(inputs.policy.option_minimum_days_to_expiry),
                maximum_days_to_expiry=(inputs.policy.option_maximum_days_to_expiry),
                target_delta=inputs.policy.option_target_delta,
                maximum_delta_distance=(inputs.policy.option_maximum_delta_distance),
                minimum_liquidity_weight=(
                    inputs.policy.option_minimum_liquidity_weight
                ),
                fallback=DeltaFallback.REJECT,
                model_specification_hash=request.valuation_model.content_hash,
            ),
            tuple(calculated_deltas),
            {item_id: item.content_hash for item_id, item in candidate_inputs.items()},
        )
        if len(selection.eligible_contract_ids) < 2:
            raise MultiAssetExperimentError(
                "builtin_option_competing_eligible_contracts_required"
            )
        if selection.selected_contract_id != command.contract_id:
            raise MultiAssetExperimentError(
                "builtin_option_command_not_selected_by_authority"
            )
        selected_implied_result = request.valuation_model.implied_volatility(
            command.valuation_input,
            permit_illiquid=request.execution_policy.allow_illiquid,
        )
        if (
            not selected_implied_result.success
            or selected_implied_result.volatility != implied
        ):
            raise MultiAssetExperimentError(
                "builtin_option_selected_model_result_mismatch"
            )
        execution = DerivativeResearchApplicationService().run_option(request)
        fill = simulate_option_fill(
            fill_id=command.order_id,
            contract=contract,
            quote=quote,
            side=command.side,
            quantity=command.quantity,
            filled_at=command.requested_at,
            participation_rate=command.participation_rate,
            fee_per_contract=request.execution_policy.fee_per_contract,
            slippage_ticks=request.execution_policy.slippage_ticks,
            allow_partial=request.execution_policy.allow_partial,
            allow_illiquid=request.execution_policy.allow_illiquid,
        )
        if fill.status not in {FillStatus.FILLED, FillStatus.PARTIAL}:
            raise MultiAssetExperimentError("builtin_option_fill_not_executed")
        position = position_from_fill(fill, position_id=command.position_id)
        mark = mark_option_position(
            position,
            quote=quote,
            theoretical_price=greeks.price,
            theoretical_input_hash=command.valuation_input.content_hash,
            marked_at=command.valuation_input.valuation_at,
            allow_illiquid=request.execution_policy.allow_illiquid,
        )
        lifecycle_command = command.lifecycle
        if lifecycle_command is None:  # pragma: no cover - narrowed above
            raise MultiAssetExperimentError("builtin_option_lifecycle_required")
        lifecycle = simulate_option_lifecycle(
            position,
            event_id=lifecycle_command.event_id,
            event_at=lifecycle_command.event_at,
            settlement_input=lifecycle_command.settlement_input,
            exercise_fraction=lifecycle_command.exercise_fraction,
            early_exercise_decision=(lifecycle_command.early_exercise_decision),
        )
        payload = execution.simulation.simulation_payload
        expected_rows = {
            "fills": fill.content_hash,
            "positions": position.content_hash,
            "implied_volatilities": selected_implied_result.content_hash,
            "greeks": greeks.content_hash,
            "marks": mark.content_hash,
            "lifecycle_events": lifecycle.content_hash,
        }
        for field_name, expected_hash in expected_rows.items():
            values = payload.get(field_name)
            if (
                not isinstance(values, list)
                or len(values) != 1
                or _mapping(
                    values[0],
                    f"builtin_option.{field_name}",
                ).get("content_hash")
                != expected_hash
            ):
                raise MultiAssetExperimentError(
                    "builtin_option_application_object_mismatch"
                )

        funding = abs(fill.cash_flow) + Decimal("1000")
        ledger = UnifiedPortfolioLedger.open(
            ledger_id=f"{context.spec.experiment_id}.builtin.option",
            base_currency=contract.currency,
        ).publish(
            funding_event(
                event_id=f"{context.spec.experiment_id}.builtin.option.funding",
                occurred_at=_timestamp_text(
                    _timestamp(fill.filled_at, "builtin_option.filled_at")
                    - timedelta(seconds=1)
                ),
                cash_deltas=(CashDelta(contract.currency, funding),),
            )
        )
        ledger = ledger.publish_many(
            adapt_option_fill(fill)  # type: ignore[arg-type]
        ).publish(adapt_option_lifecycle(lifecycle, position=position))
        snapshot = ledger.replay()
        settlement = lifecycle_command.settlement_input
        intrinsic = (
            max(
                Decimal("0"),
                settlement.spot_price - contract.strike,
            )
            if contract.option_type.value == "CALL"
            else max(
                Decimal("0"),
                contract.strike - settlement.spot_price,
            )
        )
        entry_path_mark = OptionPathMark(
            contract_id=contract.contract_id,
            marked_at=_timestamp(
                command.valuation_input.valuation_at,
                "builtin_option.valuation_at",
            ),
            market_state_hash=command.valuation_input.content_hash,
            market_quote_hash=quote.content_hash,
            model_specification_hash=request.valuation_model.content_hash,
            market_price=cast(Decimal, fill.price),
            theoretical_price=greeks.price,
            spot_price=command.valuation_input.spot_price,
            implied_volatility=implied,
            rate=command.valuation_input.risk_free_rate,
            dividend_yield=command.valuation_input.dividend_yield,
            skew=Decimal("0"),
            greeks=PathOptionGreeks(
                delta=greeks.delta,
                gamma=greeks.gamma,
                vega_per_vol_point=greeks.vega / Decimal("100"),
                theta_per_calendar_day=(greeks.theta_per_year / Decimal("365")),
                rho_per_rate_point=greeks.rho / Decimal("100"),
            ),
            transaction_cost_since_previous=fill.fee,
        )
        entry_at = _timestamp(
            command.valuation_input.valuation_at,
            "builtin_option.valuation_at",
        )
        lifecycle_at = _timestamp(
            lifecycle.occurred_at,
            "builtin_option.lifecycle_at",
        )
        intermediate_request = cast(
            OptionStudyRequest,
            inputs.option_intermediate_request.payload,
        )
        if len(intermediate_request.orders) != 1:
            raise MultiAssetExperimentError(
                "builtin_option_single_intermediate_mark_required"
            )
        intermediate_command = intermediate_request.orders[0]
        intermediate_input = intermediate_command.valuation_input
        intermediate_quote = intermediate_request.chain.quote(
            intermediate_command.contract_id
        )
        intermediate_at = _timestamp(
            intermediate_input.valuation_at,
            "builtin_option.intermediate_valuation_at",
        )
        intermediate_text = _timestamp_text(intermediate_at)
        if (
            intermediate_command.contract_id != command.contract_id
            or intermediate_input.contract.content_hash != contract.content_hash
            or intermediate_input.quote.content_hash != intermediate_quote.content_hash
            or intermediate_request.valuation_model.content_hash
            != request.valuation_model.content_hash
            or not entry_at < intermediate_at < lifecycle_at
        ):
            raise MultiAssetExperimentError(
                "builtin_option_intermediate_input_binding_mismatch"
            )
        intermediate_iv = intermediate_request.valuation_model.implied_volatility(
            intermediate_input,
            permit_illiquid=(intermediate_request.execution_policy.allow_illiquid),
        )
        if not intermediate_iv.success or intermediate_iv.volatility is None:
            raise MultiAssetExperimentError("builtin_option_intermediate_iv_required")
        intermediate_greeks = intermediate_request.valuation_model.greeks(
            intermediate_input,
            intermediate_iv.volatility,
        )
        intermediate_mark = mark_option_position(
            position,
            quote=intermediate_quote,
            theoretical_price=intermediate_greeks.price,
            theoretical_input_hash=intermediate_input.content_hash,
            marked_at=intermediate_text,
            allow_illiquid=(intermediate_request.execution_policy.allow_illiquid),
        )
        intermediate_path_mark = OptionPathMark(
            contract_id=contract.contract_id,
            marked_at=intermediate_at,
            market_state_hash=intermediate_input.content_hash,
            market_quote_hash=intermediate_quote.content_hash,
            model_specification_hash=request.valuation_model.content_hash,
            market_price=cast(Decimal, intermediate_mark.liquidation_price),
            theoretical_price=intermediate_greeks.price,
            spot_price=intermediate_input.spot_price,
            implied_volatility=intermediate_iv.volatility,
            rate=intermediate_input.risk_free_rate,
            dividend_yield=intermediate_input.dividend_yield,
            skew=Decimal("0"),
            greeks=PathOptionGreeks(
                delta=intermediate_greeks.delta,
                gamma=intermediate_greeks.gamma,
                vega_per_vol_point=(intermediate_greeks.vega / Decimal("100")),
                theta_per_calendar_day=(
                    intermediate_greeks.theta_per_year / Decimal("365")
                ),
                rho_per_rate_point=(intermediate_greeks.rho / Decimal("100")),
            ),
        )
        expiry_path_mark = OptionPathMark(
            contract_id=contract.contract_id,
            marked_at=_timestamp(
                lifecycle.occurred_at,
                "builtin_option.lifecycle_at",
            ),
            market_state_hash=settlement.content_hash,
            market_quote_hash=settlement.content_hash,
            model_specification_hash=request.valuation_model.content_hash,
            market_price=intrinsic,
            theoretical_price=intrinsic,
            spot_price=settlement.spot_price,
            implied_volatility=implied,
            rate=command.valuation_input.risk_free_rate,
            dividend_yield=command.valuation_input.dividend_yield,
            skew=Decimal("0"),
            greeks=PathOptionGreeks(
                delta=Decimal("0"),
                gamma=Decimal("0"),
                vega_per_vol_point=Decimal("0"),
                theta_per_calendar_day=Decimal("0"),
                rho_per_rate_point=Decimal("0"),
            ),
        )
        signed_quantity = (
            position.quantity
            if position.side is PositionSide.LONG
            else -position.quantity
        )
        if inputs.policy.option_maximum_absolute_residual > abs(fill.cash_flow):
            raise MultiAssetExperimentError(
                "builtin_option_absolute_residual_limit_too_loose"
            )
        attribution = attribute_option_path(
            (
                entry_path_mark,
                intermediate_path_mark,
                expiry_path_mark,
            ),
            signed_quantity=signed_quantity,
            multiplier=contract.multiplier,
            policy=OptionAttributionPolicy(
                policy_id="builtin-option-path-attribution",
                version="v1",
                maximum_absolute_residual=(
                    inputs.policy.option_maximum_absolute_residual
                ),
                maximum_relative_residual=(
                    inputs.policy.option_maximum_relative_residual
                ),
            ),
        )
        ledger_cashflow = sum(
            (
                delta.amount
                for event in ledger.events
                if event.event_type.value != "FUNDING"
                for delta in event.cash_deltas
            ),
            Decimal("0"),
        )
        competing_contracts = tuple(
            sorted(request.chain.contracts, key=lambda item: item.strike)
        )
        if len(competing_contracts) < 3:
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_requires_three_source_contracts"
            )
        competing_quotes = tuple(
            request.chain.quote(item.contract_id) for item in competing_contracts
        )
        if any(item.bid is None or item.ask is None for item in competing_quotes):
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_two_sided_quotes_required"
            )
        if any(
            right.strike <= left.strike
            for left, right in zip(
                competing_contracts,
                competing_contracts[1:],
            )
        ):
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_strike_order_invalid"
            )
        initial_observation_times = {
            _timestamp(
                item.availability.event_at,
                "builtin_option.initial_quote_event_at",
            )
            for item in competing_quotes
        }
        if len(initial_observation_times) != 1:
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_initial_clock_skew"
            )
        initial_observation_at = _timestamp_text(next(iter(initial_observation_times)))
        intermediate_contract_ids = {
            item.contract_id for item in intermediate_request.chain.contracts
        }
        if intermediate_contract_ids != {
            item.contract_id for item in competing_contracts
        }:
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_repricing_contract_mismatch"
            )
        intermediate_quotes = tuple(
            intermediate_request.chain.quote(item.contract_id)
            for item in competing_contracts
        )
        if any(item.bid is None or item.ask is None for item in intermediate_quotes):
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_repricing_quote_required"
            )
        repricing_observation_times = {
            _timestamp(
                item.availability.event_at,
                "builtin_option.repricing_quote_event_at",
            )
            for item in intermediate_quotes
        }
        if len(repricing_observation_times) != 1:
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_repricing_clock_skew"
            )
        if intermediate_request.chain.underlying_price != intermediate_input.spot_price:
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_repricing_spot_mismatch"
            )
        contract_specification = (
            contract.expiration_at,
            contract.settlement_at,
            contract.multiplier,
            contract.currency,
        )
        if any(
            (
                item.expiration_at,
                item.settlement_at,
                item.multiplier,
                item.currency,
            )
            != contract_specification
            for item in competing_contracts
        ):
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_contract_terms_mismatch"
            )
        institutional_inputs = build_public_t03_fixture_inputs(
            source_document_id=input_receipt.artifact_logical_id,
            source_document_hashes=self._institutional_source_hashes(input_receipt),
            observation_at=initial_observation_at,
            valuation_at=command.requested_at,
            knowledge_at=request.chain.knowledge_time,
            underlying_id=request.chain.underlying_id,
            contract_ids=tuple(item.contract_id for item in competing_contracts),
            strikes=tuple(item.strike for item in competing_contracts),
            quotes=tuple(
                (
                    cast(Decimal, item.bid),
                    cast(Decimal, item.ask),
                )
                for item in competing_quotes
            ),
            spot_price=request.chain.underlying_price,
            quantity=command.quantity,
            repricing_observation_at=_timestamp_text(
                next(iter(repricing_observation_times))
            ),
            repricing_knowledge_at=intermediate_request.chain.knowledge_time,
            repricing_valuation_at=intermediate_input.valuation_at,
            repricing_quotes=tuple(
                (
                    cast(Decimal, item.bid),
                    cast(Decimal, item.ask),
                )
                for item in intermediate_quotes
            ),
            repricing_spot_price=intermediate_request.chain.underlying_price,
            settlement_observation_at=settlement.availability.event_at,
            settlement_knowledge_at=settlement.availability.processed_at,
            settlement_spot_price=settlement.spot_price,
            lifecycle_event_at=lifecycle_command.event_at,
            contract_expiration_at=contract.expiration_at,
            contract_settlement_at=contract.settlement_at,
            contract_multiplier=contract.multiplier,
            contract_currency=contract.currency,
        )
        institutional_receipt = run_public_option_profile(
            receipt_id=(f"{context.spec.experiment_id}.t03.institutional.receipt"),
            inputs=institutional_inputs,
            factory=default_public_option_institutional_factory(
                fill_quantity=command.quantity,
                fill_side=command.side,
            ),
        )
        institutional_receipt.require_valid()
        if (
            institutional_receipt.selected_contract_id != command.contract_id
            or institutional_receipt.filled_quantity != command.quantity
            or institutional_receipt.executed_side is not command.side
        ):
            raise MultiAssetExperimentError(
                "builtin_option_institutional_profile_economics_mismatch"
            )
        institutional_binding = self._bind_profile_output(
            context=context,
            scenario_id="T-03",
            profile_receipt=institutional_receipt,
            profile_content_hash=institutional_receipt.content_hash,
            input_paths=(
                "/option_intermediate_request",
                "/option_request",
            ),
        )
        self._option_profile_receipts[context.repeat_index] = institutional_receipt
        objects = _scenario_objects(
            trades=(fill.as_dict(),),
            snapshot=snapshot,
            ledger_events=tuple(item.as_dict() for item in ledger.events),
            exposure={
                "position_hash": position.content_hash,
                "terminal_quantity": "0",
            },
            attribution={
                "path_attribution_hash": attribution.content_hash,
                "actual_pnl": _decimal_text(attribution.actual_pnl),
                "model_residual": _decimal_text(attribution.total_model_residual),
            },
            scenario_output={
                "derivative_simulation": execution.simulation.as_dict(),
                "cleaned_chain_hash": cleaned_chain.content_hash,
                "eligible_contract_ids": list(selection.eligible_contract_ids),
                "input_authority_receipt_hash": input_receipt.content_hash,
                "selection_hash": selection.content_hash,
                "institutional_profile_receipt": (institutional_receipt.as_dict()),
                "institutional_output_binding": (institutional_binding.as_dict()),
            },
        )
        self._option_positions[context.repeat_index] = position
        self._option_selection_evidence[context.repeat_index] = (
            tuple(sorted(selection.eligible_contract_ids)),
            selection.content_hash,
            cleaned_chain.content_hash,
        )
        return OptionScenarioTrace(
            decision_at=command.requested_at,
            maximum_chain_knowledge_at=request.chain.knowledge_time,
            chain_hash=request.chain.content_hash,
            selected_contract_id=contract.contract_id,
            selection_hash=selection.content_hash,
            entry_fill_hash=fill.content_hash,
            path_mark_hashes=(
                entry_path_mark.content_hash,
                intermediate_path_mark.content_hash,
                expiry_path_mark.content_hash,
            ),
            lifecycle_hash=lifecycle.content_hash,
            ledger_hash=ledger.content_hash,
            market_price_hash=evidence_hash(
                {
                    "quote_hash": quote.content_hash,
                    "fill_hash": fill.content_hash,
                    "price": _decimal_text(cast(Decimal, fill.price)),
                },
                label="builtin-option-market-price",
            ),
            model_price_hash=evidence_hash(
                {
                    "valuation_input_hash": (command.valuation_input.content_hash),
                    "greeks_hash": greeks.content_hash,
                    "price": _decimal_text(greeks.price),
                },
                label="builtin-option-model-price",
            ),
            premium_and_lifecycle_cashflow=(fill.cash_flow + lifecycle.cash_delta),
            ledger_option_cashflow=ledger_cashflow,
            attributed_pnl=attribution.attributed_pnl,
            actual_pnl=attribution.actual_pnl,
            accounting=_scenario_accounting(snapshot),
            object_hashes=objects,
            quality_flags=(
                "AUTHORITATIVE_DERIVATIVE_APPLICATION",
                "AUTHORITATIVE_OPTION_LIFECYCLE",
                "COMPETING_CHAIN_SELECTION",
                "IMMUTABLE_SOURCE_ROW_INPUT_AUTHORITY",
                "INTERMEDIATE_PATH_ATTRIBUTION",
                "PUBLIC_T03_INSTITUTIONAL_PROFILE",
            ),
        )

    def run_integrated(
        self,
        context: ScenarioRunContext,
        *,
        spot: SpotScenarioTrace,
        futures: FuturesScenarioTrace,
        option: OptionScenarioTrace,
    ) -> IntegratedScenarioExecution:
        inputs = self._authoritative_inputs(context)
        input_receipt = self._input_authority_receipt(context)
        stored_spot = self._spot_traces.get(context.repeat_index)
        if (
            stored_spot is None
            or replace(spot, quality_flags=stored_spot.quality_flags) != stored_spot
            or context.repeat_index not in self._spot_costs
            or context.repeat_index not in self._futures_steps
            or context.repeat_index not in self._option_positions
        ):
            raise MultiAssetExperimentError(
                "builtin_integrated_predecessor_execution_missing"
            )
        request = cast(
            MultiLegStudyRequest,
            inputs.multi_leg_request.payload,
        )
        if any(
            item.contract.settlement_type is not DerivativeOptionSettlementType.CASH
            for item in request.valuation_inputs
        ):
            raise MultiAssetExperimentError("builtin_multileg_cash_settlement_only")
        derivative_execution = DerivativeResearchApplicationService().run_multi_leg(
            request
        )
        payload = derivative_execution.simulation.simulation_payload
        market_state, registry, option_adapter = _integrated_option_market_authority(
            request=request,
            payload=payload,
            spot=inputs.spot,
        )
        exposure_engine = ExposureEngine.with_default_spot(
            product_catalog=registry,
            derivative_adapters=(option_adapter,),
        )
        bindings = (
            LedgerExposureBinding(
                instrument_id=inputs.spot.instrument_id,
                quantity_unit="unit",
                opened_at=inputs.spot.decision_at,
            ),
            *(
                LedgerExposureBinding(
                    instrument_id=leg.contract.contract_id,
                    quantity_unit="contract_unit",
                    opened_at=request.order.requested_at,
                )
                for leg in request.order.legs
            ),
        )
        command = MultiLegLedgerCommand(
            execution_id=f"{context.spec.experiment_id}.builtin.multileg",
            order=request.order,
            quotes=request.chain.quotes,
            fill_times=request.fill_times,
            participation_rates=request.participation_rates,
            fee_per_contract=request.execution_policy.fee_per_contract,
            slippage_ticks=request.execution_policy.slippage_ticks,
            allow_illiquid=request.execution_policy.allow_illiquid,
            execution_context_hash=derivative_execution.simulation.content_hash,
        )
        service = MultiLegLedgerExecutionService()
        integrated = service.execute(
            command,
            ledger=self._spot_ledgers[context.repeat_index],
            fx_rates={inputs.spot.currency: Decimal("1")},
            exposure_request=LedgerExposureRequest(
                snapshot_id=f"{context.spec.experiment_id}.builtin.exposure",
                engine=exposure_engine,
                market_state=market_state,
                bindings=bindings,
            ),
        )
        raw_multileg = _mapping(
            payload.get("multi_leg_execution"),
            "builtin_integrated.multi_leg_execution",
        )
        if integrated.authoritative_result.content_hash != raw_multileg.get(
            "content_hash"
        ):
            raise MultiAssetExperimentError(
                "builtin_multileg_application_object_mismatch"
            )
        if (
            integrated.disposition is not MultiLegDisposition.FILLED
            or integrated.exposure is None
        ):
            raise MultiAssetExperimentError("builtin_multileg_filled_exposure_required")
        entry_ledger = integrated.ledger_after
        entry_snapshot = entry_ledger.replay()
        shock = JointMarketShock(
            scenario_id=(f"{context.spec.experiment_id}.builtin.joint-shock"),
            price_returns=(
                (
                    inputs.spot.instrument_id,
                    inputs.policy.joint_spot_return,
                ),
            ),
            liquidity_haircuts=tuple(
                sorted(
                    (
                        (
                            item.instrument_id,
                            inputs.policy.joint_liquidity_haircut,
                        )
                        for item in entry_snapshot.positions
                    )
                )
            ),
            liquidity_cost_multiplier=(inputs.policy.joint_liquidity_cost_multiplier),
            margin_multiplier=inputs.policy.joint_margin_multiplier,
            source_hashes=(market_state.state_hash(),),
        )
        repricers: dict[str, _BlackScholesScenarioRepricer] = {}
        for valuation_input in request.valuation_inputs:
            implied_result = request.valuation_model.implied_volatility(
                valuation_input,
                permit_illiquid=(request.execution_policy.allow_illiquid),
            )
            if not implied_result.success or implied_result.volatility is None:
                raise MultiAssetExperimentError("builtin_multileg_scenario_iv_required")
            repricers[valuation_input.contract.contract_id] = (
                _BlackScholesScenarioRepricer(
                    model=request.valuation_model,
                    valuation_input=valuation_input,
                    volatility=implied_result.volatility,
                )
            )
        joint = JointScenarioEngine().evaluate(
            entry_snapshot,
            market_state=market_state,
            shock=shock,
            repricers=repricers,
            base_liquidation_costs={
                item.instrument_id: Decimal("0") for item in entry_snapshot.positions
            },
        )
        fills_by_id = {
            item.contract.contract_id: item
            for item in integrated.authoritative_result.committed_fills
        }
        lifecycle_commands = dict(request.lifecycle_by_contract)
        if set(lifecycle_commands) != set(fills_by_id):
            raise MultiAssetExperimentError(
                "builtin_multileg_lifecycle_coverage_required"
            )
        positions_by_contract = {
            item.contract.contract_id: item for item in integrated.positions
        }
        application_positions_by_contract = {
            fill.contract.contract_id: position_from_fill(
                fill,
                position_id=f"{request.order.group_id}.position{index}",
            )
            for index, fill in enumerate(
                integrated.authoritative_result.committed_fills
            )
        }
        lifecycle_by_id: dict[str, OptionLifecycleEvent] = {}
        application_lifecycle_by_id: dict[str, OptionLifecycleEvent] = {}
        for contract_id, lifecycle_command in sorted(lifecycle_commands.items()):
            try:
                position = positions_by_contract[contract_id]
                application_position = application_positions_by_contract[contract_id]
            except KeyError as exc:
                raise MultiAssetExperimentError(
                    "builtin_multileg_lifecycle_position_missing"
                ) from exc
            lifecycle_by_id[contract_id] = simulate_option_lifecycle(
                position,
                event_id=lifecycle_command.event_id,
                event_at=lifecycle_command.event_at,
                settlement_input=lifecycle_command.settlement_input,
                exercise_fraction=lifecycle_command.exercise_fraction,
                early_exercise_decision=(lifecycle_command.early_exercise_decision),
            )
            application_lifecycle_by_id[contract_id] = simulate_option_lifecycle(
                application_position,
                event_id=lifecycle_command.event_id,
                event_at=lifecycle_command.event_at,
                settlement_input=lifecycle_command.settlement_input,
                exercise_fraction=lifecycle_command.exercise_fraction,
                early_exercise_decision=(lifecycle_command.early_exercise_decision),
            )
        raw_lifecycle = payload.get("lifecycle_events")
        if not isinstance(raw_lifecycle, list) or {
            _mapping(
                item,
                "builtin_integrated.lifecycle_event",
            ).get("content_hash")
            for item in raw_lifecycle
        } != {item.content_hash for item in application_lifecycle_by_id.values()}:
            raise MultiAssetExperimentError(
                "builtin_multileg_lifecycle_object_mismatch"
            )
        if any(
            _lifecycle_economic_payload(lifecycle_by_id[contract_id])
            != _lifecycle_economic_payload(application_lifecycle_by_id[contract_id])
            for contract_id in lifecycle_by_id
        ):
            raise MultiAssetExperimentError(
                "builtin_multileg_lifecycle_economics_mismatch"
            )
        lifecycle_projection = service.project_lifecycle(
            integrated,
            events=tuple(lifecycle_by_id.values()),
            deliverable_asset_classes={},
        )
        ledger = lifecycle_projection.ledger_after
        snapshot = ledger.replay()
        if {item.instrument_id for item in snapshot.positions} != {
            inputs.spot.instrument_id
        }:
            raise MultiAssetExperimentError(
                "builtin_multileg_terminal_residual_position_mismatch"
            )
        receipt = _report_ledger_reconciliation(
            ledger,
            reconciliation_id=(f"{context.spec.experiment_id}.builtin.reconciliation"),
        )
        accounting = ScenarioAccounting(
            opening_nav=receipt.ledger.opening_nav,
            external_cash_flow=receipt.ledger.external_cash_flow,
            closing_nav=receipt.ledger.closing_nav,
            ledger_pnl=receipt.ledger.ledger_event_pnl,
            report_pnl=receipt.report.ledger_pnl,
        )
        position_by_id = {item.instrument_id: item for item in snapshot.positions}
        option_pnls = {
            contract_id: (fill.cash_flow + lifecycle_by_id[contract_id].cash_delta)
            for contract_id, fill in fills_by_id.items()
        }
        spot_pnl = accounting.ledger_pnl - sum(
            option_pnls.values(),
            Decimal("0"),
        )
        legs: list[IntegratedLegResult] = [
            IntegratedLegResult(
                leg_id="builtin.spot",
                instrument_id=inputs.spot.instrument_id,
                trade_hash=spot.trade_hashes[0],
                cost=self._spot_costs[context.repeat_index],
                pnl=spot_pnl,
                terminal_quantity=position_by_id[inputs.spot.instrument_id].quantity,
            )
        ]
        for index, (contract_id, fill) in enumerate(sorted(fills_by_id.items())):
            legs.append(
                IntegratedLegResult(
                    leg_id=f"builtin.option.{index}",
                    instrument_id=contract_id,
                    trade_hash=fill.content_hash,
                    cost=fill.fee,
                    pnl=option_pnls[contract_id],
                    terminal_quantity=Decimal("0"),
                )
            )
        public_legs = tuple(
            sorted(
                request.order.legs,
                key=lambda item: item.contract.contract_id,
            )
        )
        if len(public_legs) != 2:
            raise MultiAssetExperimentError(
                "builtin_integrated_institutional_profile_requires_two_legs"
            )
        public_prices: list[Decimal] = []
        target_notional = Decimal("0")
        for item in public_legs:
            public_quote = request.chain.quote(item.contract.contract_id)
            public_midpoint = public_quote.midpoint
            if public_midpoint is None:
                raise MultiAssetExperimentError(
                    "builtin_integrated_institutional_profile_quote_required"
                )
            public_prices.append(public_midpoint)
            target_notional += (
                abs(item.quantity) * public_midpoint * item.contract.multiplier
            )
        funding_currency = "EUR" if inputs.spot.currency != "EUR" else "USD"
        public_directions = cast(
            tuple[Direction, Direction],
            tuple(
                Direction.LONG if item.side is PositionSide.LONG else Direction.SHORT
                for item in public_legs
            ),
        )
        institutional_receipt = run_public_integrated_profile(
            build_public_t04_fixture_inputs(
                source_document_id=input_receipt.artifact_logical_id,
                source_document_hashes=self._institutional_source_hashes(input_receipt),
                opened_at=request.started_at,
                closed_at=request.finished_at,
                underlying_id=inputs.spot.instrument_id,
                leg_instrument_ids=cast(
                    tuple[str, str],
                    tuple(item.contract.contract_id for item in public_legs),
                ),
                leg_prices=cast(
                    tuple[Decimal, Decimal],
                    tuple(public_prices),
                ),
                leg_directions=public_directions,
                target_notional=target_notional,
                base_currency=inputs.spot.currency,
                funding_currency=funding_currency,
            )
        )
        expected_public_instruments = tuple(
            item.contract.contract_id for item in public_legs
        )
        expected_public_directions = tuple(
            sorted(
                (
                    item.contract.contract_id,
                    (
                        Direction.LONG.value
                        if item.side is PositionSide.LONG
                        else Direction.SHORT.value
                    ),
                )
                for item in public_legs
            )
        )
        if (
            institutional_receipt.selected_instruments != expected_public_instruments
            or institutional_receipt.selected_directions != expected_public_directions
        ):
            raise MultiAssetExperimentError(
                "builtin_integrated_institutional_profile_economics_mismatch"
            )
        institutional_binding = self._bind_profile_output(
            context=context,
            scenario_id="T-04",
            profile_receipt=institutional_receipt,
            profile_content_hash=institutional_receipt.content_hash,
            input_paths=("/multi_leg_request", "/spot"),
        )
        self._integrated_profile_receipts[context.repeat_index] = institutional_receipt
        try:
            profile_bindings = self._profile_output_bindings[context.repeat_index]
            execution_evidence = build_public_execution_evidence_bundle(
                experiment_spec_hash=context.spec.content_hash,
                authoritative_input_receipt=input_receipt,
                spot_profile_receipt=self._spot_profile_receipts[context.repeat_index],
                futures_profile_receipt=self._futures_profile_receipts[
                    context.repeat_index
                ],
                option_profile_receipt=self._option_profile_receipts[
                    context.repeat_index
                ],
                integrated_profile_receipt=institutional_receipt,
                output_bindings=tuple(
                    profile_bindings[scenario_id]
                    for scenario_id in ("T-01", "T-02", "T-03", "T-04")
                ),
            )
        except KeyError as exc:
            raise MultiAssetExperimentError(
                "builtin_institutional_profile_evidence_incomplete"
            ) from exc
        self._execution_evidence_bundles[context.repeat_index] = execution_evidence
        objects = _scenario_objects(
            trades=(
                integrated.authoritative_result.identity_payload(),
                integrated.as_dict(),
            ),
            snapshot=snapshot,
            ledger_events=tuple(item.as_dict() for item in ledger.events),
            exposure=integrated.exposure.as_dict(),
            attribution={
                "leg_pnl": [
                    {
                        "instrument_id": item.instrument_id,
                        "pnl": _decimal_text(item.pnl),
                        "cost": _decimal_text(item.cost),
                    }
                    for item in legs
                ],
                "ledger_reconciliation_hash": receipt.content_hash,
            },
            scenario_output={
                "joint_scenario": joint.identity_payload(),
                "multi_leg_execution": integrated.as_dict(),
                "multi_leg_lifecycle": (lifecycle_projection.as_dict()),
                "input_authority_receipt_hash": input_receipt.content_hash,
                "institutional_profile_receipt": (institutional_receipt.as_dict()),
                "institutional_output_binding": (institutional_binding.as_dict()),
                "public_execution_evidence_bundle_hash": (
                    execution_evidence.content_hash
                ),
            },
        )
        trace = IntegratedScenarioTrace(
            execution_mode=integrated.execution_mode,
            legs=tuple(legs),
            common_ledger_hash=ledger.content_hash,
            ledger_reconciled=(
                receipt.ledger.nav_identity_error == 0
                and receipt.ledger.attribution_identity_error == 0
            ),
            exposure_hash=integrated.exposure.content_hash,
            exposure_reconciled=all(
                item.expected == item.actual
                for item in integrated.exposure.evidence.invariant_checks
            ),
            scenario_result_hash=joint.content_hash,
            scenario_repriced=(
                joint.original_state_unchanged
                and any(
                    item.base_mark != item.shocked_mark
                    for item in joint.position_results
                )
            ),
            strategy_pnl=sum((item.pnl for item in legs), Decimal("0")),
            accounting=accounting,
            object_hashes=objects,
            quality_flags=(
                "AUTHORITATIVE_EXPOSURE_ENGINE",
                "AUTHORITATIVE_JOINT_SCENARIO_ENGINE",
                "AUTHORITATIVE_MULTILEG_COMMON_LEDGER",
                "AUTHORITATIVE_MULTILEG_LIFECYCLE",
                "IMMUTABLE_SOURCE_ROW_INPUT_AUTHORITY",
                "PUBLIC_T04_INSTITUTIONAL_PROFILE",
            ),
        )
        return IntegratedScenarioExecution(
            trace=trace,
            accounting_reconciliation=receipt,
        )


@dataclass(frozen=True, slots=True)
class _BlackScholesScenarioRepricer:
    """Re-run the bound model under the shocked economic underlying."""

    model: BlackScholesModel
    valuation_input: ValuationInputSnapshot
    volatility: Decimal

    def reprice(
        self,
        position: PositionView,
        *,
        market_state: object,
        shocked_state: ShockedMarketState,
    ) -> Decimal:
        del market_state
        if position.instrument_id != self.valuation_input.contract.contract_id:
            raise MultiAssetExperimentError(
                "builtin_option_repricer_instrument_mismatch"
            )
        shocked_spot = shocked_state.price_for(
            self.valuation_input.contract.underlying_id
        )
        shocked_forward = shocked_spot * Decimal(
            str(
                math.exp(
                    float(
                        (
                            self.valuation_input.risk_free_rate
                            - self.valuation_input.dividend_yield
                        )
                        * self.valuation_input.time_to_expiry_years
                    )
                )
            )
        )
        shocked_input = replace(
            self.valuation_input,
            valuation_input_id=(
                f"{self.valuation_input.valuation_input_id}.builtin-joint-shock"
            ),
            spot_price=shocked_spot,
            forward_price=shocked_forward,
        )
        return self.model.greeks(shocked_input, self.volatility).price


def _integrated_option_market_authority(
    *,
    request: MultiLegStudyRequest,
    payload: Mapping[str, object],
    spot: BuiltinSpotScenarioInput,
) -> tuple[MarketState, InstrumentRegistry, OptionValuationAdapter]:
    """Rebuild the common option state from the authoritative model outputs."""

    valuation_times = {item.valuation_at for item in request.valuation_inputs}
    if len(valuation_times) != 1:
        raise MultiAssetExperimentError(
            "builtin_multileg_common_valuation_time_required"
        )
    valuation_at = next(iter(valuation_times))
    inputs = {item.contract.contract_id: item for item in request.valuation_inputs}
    if set(inputs) != {item.contract.contract_id for item in request.order.legs}:
        raise MultiAssetExperimentError(
            "builtin_multileg_valuation_input_coverage_mismatch"
        )
    model = request.valuation_model
    pricing_adapter = BlackScholesPricingAdapter(
        model=model,
        specification=black_scholes_pricing_specification(model.model_version),
    )
    analytics_factory = AuthoritativeOptionAnalyticsFactory(
        registry=default_option_model_registry(),
        comparison_policy=default_analytics_comparison_policy(),
        margin_model_hash=request.execution_policy.content_hash,
        pricing_adapter=pricing_adapter,
    )
    calculated: dict[str, tuple[Decimal, OptionGreeks]] = {}
    for contract_id, item in inputs.items():
        implied = model.implied_volatility(
            item,
            permit_illiquid=request.execution_policy.allow_illiquid,
        )
        if not implied.success or implied.volatility is None:
            raise MultiAssetExperimentError(
                "builtin_multileg_implied_volatility_required"
            )
        calculated[contract_id] = (
            implied.volatility,
            model.greeks(item, implied.volatility),
        )
    raw_ivs = payload.get("implied_volatilities")
    raw_greeks = payload.get("greeks")
    if not isinstance(raw_ivs, list) or not isinstance(raw_greeks, list):
        raise MultiAssetExperimentError("builtin_multileg_model_outputs_required")
    expected_iv_hashes = {
        _mapping(item, "builtin_multileg.iv").get("content_hash") for item in raw_ivs
    }
    expected_greek_hashes = {
        _mapping(item, "builtin_multileg.greeks").get("content_hash")
        for item in raw_greeks
    }
    actual_iv_hashes = {
        model.implied_volatility(
            item,
            permit_illiquid=request.execution_policy.allow_illiquid,
        ).content_hash
        for item in request.valuation_inputs
    }
    actual_greek_hashes = {values[1].content_hash for values in calculated.values()}
    if (
        expected_iv_hashes != actual_iv_hashes
        or expected_greek_hashes != actual_greek_hashes
    ):
        raise MultiAssetExperimentError("builtin_multileg_model_object_mismatch")

    calendar_id = "builtin-offline-calendar"
    metadata = ObservationMetadata(
        observed_at=valuation_at,
        knowledge_at=valuation_at,
        source_hash=request.chain.content_hash,
        calendar_id=calendar_id,
        max_age_seconds=0,
        quality=MarketDataQuality.GOOD,
    )
    option_quotes: list[OptionContractQuote] = []
    analytics: list[OptionAnalyticsMark] = []
    liquidity: list[LiquidityQuote] = []
    quote_by_id = {item.contract_id: item for item in request.chain.quotes}
    for contract_id, valuation_input in sorted(inputs.items()):
        contract = valuation_input.contract
        quote = quote_by_id[contract_id]
        if quote.bid is None or quote.ask is None:
            raise MultiAssetExperimentError("builtin_multileg_two_sided_quote_required")
        quote_metadata = ObservationMetadata(
            observed_at=quote.availability.event_at,
            knowledge_at=quote.availability.processed_at,
            source_hash=quote.content_hash,
            calendar_id=calendar_id,
            max_age_seconds=quote.stale_after_seconds,
            quality=(
                MarketDataQuality.INDICATIVE
                if quote.state is QuoteState.ILLIQUID
                else MarketDataQuality.GOOD
            ),
        )
        common_quote = OptionContractQuote(
            contract_id=contract_id,
            underlying_instrument_id=spot.instrument_id,
            expiry_at=contract.expiration_at,
            right=OptionRight(contract.option_type.value),
            strike=contract.strike,
            currency=contract.currency,
            price_unit=f"{contract.currency}_per_contract_unit",
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last,
            settlement=None,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            volume=Decimal(quote.volume),
            open_interest=Decimal(quote.open_interest),
            condition=(
                QuoteCondition.INDICATIVE
                if quote.state is QuoteState.ILLIQUID
                else QuoteCondition.NORMAL
            ),
            metadata=quote_metadata,
        )
        option_quotes.append(common_quote)
        receipt = analytics_factory.derive(
            receipt_id=f"{contract_id}.builtin-authoritative-analytics",
            quote=common_quote,
            valuation_input=valuation_input,
            margin_per_contract=Decimal("0"),
            collateral_per_contract=Decimal("0"),
            permit_illiquid=request.execution_policy.allow_illiquid,
        )
        analytics.append(receipt.analytics_mark)
        liquidity.append(
            LiquidityQuote(
                instrument_id=contract_id,
                currency=contract.currency,
                bid=quote.bid,
                ask=quote.ask,
                price_unit=f"{contract.currency}_per_contract_unit",
                depth_quantity=min(quote.bid_size, quote.ask_size),
                quantity_unit="contract_unit",
                metadata=metadata,
            )
        )
    spot_price = request.valuation_inputs[0].spot_price
    spot_quote = SpotQuote(
        instrument_id=spot.instrument_id,
        price=spot_price,
        currency=spot.currency,
        unit=f"{spot.currency}_per_unit",
        metadata=metadata,
    )
    liquidity.append(
        LiquidityQuote(
            instrument_id=spot.instrument_id,
            currency=spot.currency,
            bid=spot_price * Decimal("0.999"),
            ask=spot_price * Decimal("1.001"),
            price_unit=f"{spot.currency}_per_unit",
            depth_quantity=spot.quantity * spot.split_ratio * Decimal("100"),
            quantity_unit="unit",
            metadata=metadata,
        )
    )
    chain_state = OptionChainState(
        chain_id=f"{request.chain.chain_snapshot_id}.common-state",
        underlying_instrument_id=spot.instrument_id,
        currency=spot.currency,
        price_unit=f"{spot.currency}_per_contract_unit",
        quotes=tuple(option_quotes),
        analytics=tuple(analytics),
        metadata=metadata,
    )
    market_state = MarketState(
        state_id=f"{request.chain.chain_snapshot_id}.market-state",
        valuation_at=valuation_at,
        base_currency=spot.currency,
        calendar_ids=(calendar_id,),
        spots=(spot_quote,),
        liquidity_quotes=tuple(liquidity),
        option_chains=(chain_state,),
    )

    source = SourceReference(
        source_id="builtin-derivative-request",
        source_version="v1",
        content_hash=request.chain.content_hash,
        observed_at=min(item.contract.listing_at for item in request.valuation_inputs),
    )
    open_period = EffectivePeriod(
        min(item.contract.listing_at for item in request.valuation_inputs),
        None,
    )
    instruments: list[Instrument] = [
        Instrument(
            instrument_id=spot.instrument_id,
            kind=InstrumentKind.SPOT,
            name=f"Builtin underlying {spot.instrument_id}",
            economic_underlying_id=spot.economic_underlying_id,
            currency=spot.currency,
            unit="unit",
            validity=open_period,
            source=source,
        )
    ]
    specifications: list[ContractSpecification] = []
    relationships: list[InstrumentRelationship] = []
    for contract in sorted(
        (item.contract for item in request.valuation_inputs),
        key=lambda item: item.contract_id,
    ):
        period = EffectivePeriod(contract.listing_at, contract.settlement_at)
        instruments.append(
            Instrument(
                instrument_id=contract.contract_id,
                kind=InstrumentKind.OPTION,
                name=f"Builtin option {contract.contract_id}",
                economic_underlying_id=spot.economic_underlying_id,
                currency=contract.currency,
                unit="contract_unit",
                validity=period,
                source=source,
            )
        )
        specifications.append(
            ContractSpecification(
                contract_specification_id=(f"{contract.contract_id}.builtin-spec"),
                instrument_id=contract.contract_id,
                contract_multiplier=contract.multiplier,
                contract_unit="contract_unit",
                settlement_type=(
                    SettlementType.CASH
                    if contract.settlement_type is DerivativeOptionSettlementType.CASH
                    else SettlementType.PHYSICAL
                ),
                settlement_currency=contract.currency,
                expiry_at=contract.expiration_at,
                last_trade_at=contract.last_trade_at,
                exercise_style=contract.exercise_style.value,
                minimum_tick=contract.price_tick,
                tick_value=contract.price_tick * contract.multiplier,
                trading_currency=contract.currency,
                calendar_id=calendar_id,
                lifecycle_rule_id="builtin-option-lifecycle",
                validity=period,
                source=source,
            )
        )
        relationships.append(
            InstrumentRelationship(
                relationship_id=(f"{contract.contract_id}.builtin-underlying"),
                source_instrument_id=contract.contract_id,
                target_instrument_id=spot.instrument_id,
                relationship_type=InstrumentRelationshipType.OPTION_UNDERLYING,
                quantity_ratio=Decimal("1"),
                validity=period,
                source=source,
            )
        )
    registry = InstrumentRegistry(
        economic_underlyings=(
            EconomicUnderlying(
                underlying_id=spot.economic_underlying_id,
                name=f"Builtin economic underlying {spot.economic_underlying_id}",
                asset_class="SPOT",
                unit="unit",
                currency=spot.currency,
                validity=open_period,
                source=source,
            ),
        ),
        instruments=tuple(instruments),
        contract_specifications=tuple(specifications),
        relationships=tuple(relationships),
    )
    adapter = OptionValuationAdapter(
        pricing_model_hash=pricing_adapter.model.content_hash,
        model_specification_hash=pricing_adapter.specification.content_hash,
        margin_model_hash=request.execution_policy.content_hash,
    )
    return market_state, registry, adapter


@dataclass(frozen=True, slots=True)
class BuiltinExecutionRecord:
    """Immutable public receipt for one successful built-in application run."""

    request_hash: str
    run_id: str
    experiment_spec_hash: str
    study_content_hash: str
    run_manifest_hash: str
    study_artifact_hash: str
    report_artifact_hash: str
    manifest_artifact_hash: str
    option_eligible_contract_ids: tuple[str, ...]
    option_selection_hash: str
    option_cleaned_chain_hash: str
    public_profile_receipt_hashes: tuple[str, ...]
    public_execution_evidence_hash: str
    public_execution_evidence_artifact_hash: str
    portable_package_manifest_hash: str
    content_hash: str = field(init=False)
    schema_version: int = BUILTIN_MULTI_ASSET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BUILTIN_MULTI_ASSET_SCHEMA_VERSION:
            raise BuiltinMultiAssetCodecError("builtin_execution_schema_unsupported")
        _require_id(self.run_id, "builtin_execution.run_id")
        for field_name in (
            "request_hash",
            "experiment_spec_hash",
            "study_content_hash",
            "run_manifest_hash",
            "study_artifact_hash",
            "report_artifact_hash",
            "manifest_artifact_hash",
            "option_selection_hash",
            "option_cleaned_chain_hash",
            "public_execution_evidence_hash",
            "public_execution_evidence_artifact_hash",
            "portable_package_manifest_hash",
        ):
            _require_hash(
                getattr(self, field_name),
                f"builtin_execution.{field_name}",
            )
        if len(
            self.option_eligible_contract_ids
        ) < 2 or self.option_eligible_contract_ids != tuple(
            sorted(set(self.option_eligible_contract_ids))
        ):
            raise BuiltinMultiAssetCodecError(
                "builtin_execution_option_eligible_contracts_invalid"
            )
        for contract_id in self.option_eligible_contract_ids:
            _require_id(
                contract_id,
                "builtin_execution.option_eligible_contract_id",
            )
        if len(self.public_profile_receipt_hashes) != 4:
            raise BuiltinMultiAssetCodecError(
                "builtin_execution_public_profile_receipt_count_invalid"
            )
        for receipt_hash in self.public_profile_receipt_hashes:
            _require_hash(
                receipt_hash,
                "builtin_execution.public_profile_receipt_hash",
            )
        object.__setattr__(
            self,
            "content_hash",
            _identity_hash(
                self.identity_payload(),
                label="builtin-multi-asset-execution",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": _EXECUTION_ARTIFACT_TYPE,
            "request_hash": self.request_hash,
            "run_id": self.run_id,
            "experiment_spec_hash": self.experiment_spec_hash,
            "study_content_hash": self.study_content_hash,
            "run_manifest_hash": self.run_manifest_hash,
            "study_artifact_hash": self.study_artifact_hash,
            "report_artifact_hash": self.report_artifact_hash,
            "manifest_artifact_hash": self.manifest_artifact_hash,
            "option_eligible_contract_ids": list(self.option_eligible_contract_ids),
            "option_selection_hash": self.option_selection_hash,
            "option_cleaned_chain_hash": self.option_cleaned_chain_hash,
            "public_profile_receipt_hashes": list(self.public_profile_receipt_hashes),
            "public_execution_evidence_hash": (self.public_execution_evidence_hash),
            "public_execution_evidence_artifact_hash": (
                self.public_execution_evidence_artifact_hash
            ),
            "portable_package_manifest_hash": (self.portable_package_manifest_hash),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> BuiltinExecutionRecord:
        payload = _mapping(value, "builtin_execution")
        _exact_fields(
            payload,
            {
                "schema_version",
                "artifact_type",
                "request_hash",
                "run_id",
                "experiment_spec_hash",
                "study_content_hash",
                "run_manifest_hash",
                "study_artifact_hash",
                "report_artifact_hash",
                "manifest_artifact_hash",
                "option_eligible_contract_ids",
                "option_selection_hash",
                "option_cleaned_chain_hash",
                "public_profile_receipt_hashes",
                "public_execution_evidence_hash",
                "public_execution_evidence_artifact_hash",
                "portable_package_manifest_hash",
                "content_hash",
            },
            "builtin_execution",
        )
        if payload["artifact_type"] != _EXECUTION_ARTIFACT_TYPE:
            raise BuiltinMultiAssetCodecError("builtin_execution_artifact_type_invalid")
        raw_eligible = payload["option_eligible_contract_ids"]
        raw_profile_hashes = payload["public_profile_receipt_hashes"]
        if not isinstance(raw_eligible, list):
            raise BuiltinMultiAssetCodecError(
                "builtin_execution_option_eligible_contracts_array_required"
            )
        if not isinstance(raw_profile_hashes, list):
            raise BuiltinMultiAssetCodecError(
                "builtin_execution_public_profile_receipts_array_required"
            )
        result = cls(
            schema_version=_integer(
                payload["schema_version"],
                "builtin_execution.schema_version",
            ),
            request_hash=_text(
                payload["request_hash"],
                "builtin_execution.request_hash",
            ),
            run_id=_text(payload["run_id"], "builtin_execution.run_id"),
            experiment_spec_hash=_text(
                payload["experiment_spec_hash"],
                "builtin_execution.experiment_spec_hash",
            ),
            study_content_hash=_text(
                payload["study_content_hash"],
                "builtin_execution.study_content_hash",
            ),
            run_manifest_hash=_text(
                payload["run_manifest_hash"],
                "builtin_execution.run_manifest_hash",
            ),
            study_artifact_hash=_text(
                payload["study_artifact_hash"],
                "builtin_execution.study_artifact_hash",
            ),
            report_artifact_hash=_text(
                payload["report_artifact_hash"],
                "builtin_execution.report_artifact_hash",
            ),
            manifest_artifact_hash=_text(
                payload["manifest_artifact_hash"],
                "builtin_execution.manifest_artifact_hash",
            ),
            option_eligible_contract_ids=tuple(
                _text(
                    item,
                    "builtin_execution.option_eligible_contract_id",
                )
                for item in raw_eligible
            ),
            option_selection_hash=_text(
                payload["option_selection_hash"],
                "builtin_execution.option_selection_hash",
            ),
            option_cleaned_chain_hash=_text(
                payload["option_cleaned_chain_hash"],
                "builtin_execution.option_cleaned_chain_hash",
            ),
            public_profile_receipt_hashes=tuple(
                _text(
                    item,
                    "builtin_execution.public_profile_receipt_hash",
                )
                for item in raw_profile_hashes
            ),
            public_execution_evidence_hash=_text(
                payload["public_execution_evidence_hash"],
                "builtin_execution.public_execution_evidence_hash",
            ),
            public_execution_evidence_artifact_hash=_text(
                payload["public_execution_evidence_artifact_hash"],
                "builtin_execution.public_execution_evidence_artifact_hash",
            ),
            portable_package_manifest_hash=_text(
                payload["portable_package_manifest_hash"],
                "builtin_execution.portable_package_manifest_hash",
            ),
        )
        if result.content_hash != _text(
            payload["content_hash"],
            "builtin_execution.content_hash",
        ):
            raise BuiltinMultiAssetCodecError("builtin_execution_content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class BuiltinReproductionRecord:
    """Immutable comparison receipt for a public built-in reproduction."""

    request_hash: str
    expected_execution_hash: str
    reproduction_run_id: str
    expected_study_hash: str
    reproduced_study_hash: str
    reproduced_manifest_hash: str
    expected_public_execution_evidence_hash: str
    reproduced_public_execution_evidence_hash: str
    reproduced_public_execution_evidence_artifact_hash: str
    expected_portable_package_manifest_hash: str
    reproduced_portable_package_manifest_hash: str
    status: BuiltinReproductionStatus
    mismatch_fields: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = BUILTIN_MULTI_ASSET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BUILTIN_MULTI_ASSET_SCHEMA_VERSION:
            raise BuiltinMultiAssetCodecError("builtin_reproduction_schema_unsupported")
        _require_id(
            self.reproduction_run_id,
            "builtin_reproduction.reproduction_run_id",
        )
        for field_name in (
            "request_hash",
            "expected_execution_hash",
            "expected_study_hash",
            "reproduced_study_hash",
            "reproduced_manifest_hash",
            "expected_public_execution_evidence_hash",
            "reproduced_public_execution_evidence_hash",
            "reproduced_public_execution_evidence_artifact_hash",
            "expected_portable_package_manifest_hash",
            "reproduced_portable_package_manifest_hash",
        ):
            _require_hash(
                getattr(self, field_name),
                f"builtin_reproduction.{field_name}",
            )
        if self.mismatch_fields != tuple(sorted(set(self.mismatch_fields))):
            raise BuiltinMultiAssetCodecError(
                "builtin_reproduction_mismatches_not_sorted_unique"
            )
        for value in self.mismatch_fields:
            _require_id(value, "builtin_reproduction.mismatch_field")
        if (self.status is BuiltinReproductionStatus.PASS) != (
            not self.mismatch_fields
        ):
            raise BuiltinMultiAssetCodecError("builtin_reproduction_status_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _identity_hash(
                self.identity_payload(),
                label="builtin-multi-asset-reproduction",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": _REPRODUCTION_ARTIFACT_TYPE,
            "request_hash": self.request_hash,
            "expected_execution_hash": self.expected_execution_hash,
            "reproduction_run_id": self.reproduction_run_id,
            "expected_study_hash": self.expected_study_hash,
            "reproduced_study_hash": self.reproduced_study_hash,
            "reproduced_manifest_hash": self.reproduced_manifest_hash,
            "expected_public_execution_evidence_hash": (
                self.expected_public_execution_evidence_hash
            ),
            "reproduced_public_execution_evidence_hash": (
                self.reproduced_public_execution_evidence_hash
            ),
            "reproduced_public_execution_evidence_artifact_hash": (
                self.reproduced_public_execution_evidence_artifact_hash
            ),
            "expected_portable_package_manifest_hash": (
                self.expected_portable_package_manifest_hash
            ),
            "reproduced_portable_package_manifest_hash": (
                self.reproduced_portable_package_manifest_hash
            ),
            "status": self.status.value,
            "mismatch_fields": list(self.mismatch_fields),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> BuiltinReproductionRecord:
        payload = _mapping(value, "builtin_reproduction")
        _exact_fields(
            payload,
            {
                "schema_version",
                "artifact_type",
                "request_hash",
                "expected_execution_hash",
                "reproduction_run_id",
                "expected_study_hash",
                "reproduced_study_hash",
                "reproduced_manifest_hash",
                "expected_public_execution_evidence_hash",
                "reproduced_public_execution_evidence_hash",
                "reproduced_public_execution_evidence_artifact_hash",
                "expected_portable_package_manifest_hash",
                "reproduced_portable_package_manifest_hash",
                "status",
                "mismatch_fields",
                "content_hash",
            },
            "builtin_reproduction",
        )
        if payload["artifact_type"] != _REPRODUCTION_ARTIFACT_TYPE:
            raise BuiltinMultiAssetCodecError(
                "builtin_reproduction_artifact_type_invalid"
            )
        raw_mismatches = payload["mismatch_fields"]
        if not isinstance(raw_mismatches, list):
            raise BuiltinMultiAssetCodecError(
                "builtin_reproduction_mismatch_array_required"
            )
        try:
            status = BuiltinReproductionStatus(
                _text(
                    payload["status"],
                    "builtin_reproduction.status",
                )
            )
        except ValueError as exc:
            raise BuiltinMultiAssetCodecError(
                "builtin_reproduction_status_invalid"
            ) from exc
        result = cls(
            schema_version=_integer(
                payload["schema_version"],
                "builtin_reproduction.schema_version",
            ),
            request_hash=_text(
                payload["request_hash"],
                "builtin_reproduction.request_hash",
            ),
            expected_execution_hash=_text(
                payload["expected_execution_hash"],
                "builtin_reproduction.expected_execution_hash",
            ),
            reproduction_run_id=_text(
                payload["reproduction_run_id"],
                "builtin_reproduction.reproduction_run_id",
            ),
            expected_study_hash=_text(
                payload["expected_study_hash"],
                "builtin_reproduction.expected_study_hash",
            ),
            reproduced_study_hash=_text(
                payload["reproduced_study_hash"],
                "builtin_reproduction.reproduced_study_hash",
            ),
            reproduced_manifest_hash=_text(
                payload["reproduced_manifest_hash"],
                "builtin_reproduction.reproduced_manifest_hash",
            ),
            expected_public_execution_evidence_hash=_text(
                payload["expected_public_execution_evidence_hash"],
                ("builtin_reproduction.expected_public_execution_evidence_hash"),
            ),
            reproduced_public_execution_evidence_hash=_text(
                payload["reproduced_public_execution_evidence_hash"],
                ("builtin_reproduction.reproduced_public_execution_evidence_hash"),
            ),
            reproduced_public_execution_evidence_artifact_hash=_text(
                payload["reproduced_public_execution_evidence_artifact_hash"],
                (
                    "builtin_reproduction."
                    "reproduced_public_execution_evidence_artifact_hash"
                ),
            ),
            expected_portable_package_manifest_hash=_text(
                payload["expected_portable_package_manifest_hash"],
                ("builtin_reproduction.expected_portable_package_manifest_hash"),
            ),
            reproduced_portable_package_manifest_hash=_text(
                payload["reproduced_portable_package_manifest_hash"],
                ("builtin_reproduction.reproduced_portable_package_manifest_hash"),
            ),
            status=status,
            mismatch_fields=tuple(
                _text(item, "builtin_reproduction.mismatch_field")
                for item in raw_mismatches
            ),
        )
        if result.content_hash != _text(
            payload["content_hash"],
            "builtin_reproduction.content_hash",
        ):
            raise BuiltinMultiAssetCodecError(
                "builtin_reproduction_content_hash_mismatch"
            )
        return result


def load_builtin_multi_asset_request(
    paths: ResearchPathManager,
    path: str | Path,
) -> BuiltinMultiAssetRequest:
    return BuiltinMultiAssetRequest.from_dict(
        read_external_derivative_json(
            path,
            paths,
            "multi_asset_builtin_request",
        )
    )


def write_builtin_multi_asset_request(
    paths: ResearchPathManager,
    path: str | Path,
    request: BuiltinMultiAssetRequest,
) -> Path:
    return write_external_derivative_json(
        path,
        paths,
        request.as_dict(),
        "multi_asset_builtin_request",
    )


def load_builtin_execution_record(
    paths: ResearchPathManager,
    path: str | Path,
) -> BuiltinExecutionRecord:
    return BuiltinExecutionRecord.from_dict(
        read_external_derivative_json(
            path,
            paths,
            "multi_asset_builtin_execution",
        )
    )


def _builtin_public_cards(
    *,
    request: BuiltinMultiAssetRequest,
    bundle: PublicExecutionEvidenceBundle,
) -> tuple[DataCard, tuple[ModelCard, ...]]:
    data_validation = CardValidationResult(
        check_id="immutable_source_row_coverage",
        status=ValidationStatus.PASS,
        summary=(
            "Every canonical public input leaf is covered by a source row "
            "known by the decision cutoff."
        ),
        evidence_hash=bundle.authoritative_input_receipt.coverage_hash,
    )
    model_validation = CardValidationResult(
        check_id="public_profile_repeated_execution",
        status=ValidationStatus.PASS,
        summary=(
            "T-01 through T-04 public institutional profiles produced one "
            "closed deterministic evidence bundle."
        ),
        evidence_hash=bundle.content_hash,
    )
    model_diagnostic = CardValidationResult(
        check_id="profile_diagnostics_reconciled",
        status=ValidationStatus.PASS,
        summary=(
            "All four public profile receipts passed their typed economic and "
            "accounting invariants."
        ),
        evidence_hash=evidence_hash(
            [
                bundle.spot_profile_receipt.content_hash,
                bundle.futures_profile_receipt.content_hash,
                bundle.option_profile_receipt.content_hash,
                bundle.integrated_profile_receipt.content_hash,
            ],
            label="builtin-public-profile-diagnostics",
        ),
    )
    model_convergence = CardValidationResult(
        check_id="deterministic_repeat_convergence",
        status=ValidationStatus.PASS,
        summary=(
            "Two executions converged to identical profile and study object "
            "hashes with no tolerance-based substitution."
        ),
        evidence_hash=bundle.content_hash,
    )
    model_benchmark = CardValidationResult(
        check_id="baseline_reference_comparison",
        status=ValidationStatus.PASS,
        summary=(
            "The supported study includes buy-and-hold and no-op reference "
            "strategies under the same immutable evidence bindings."
        ),
        evidence_hash=request.spec.content_hash,
    )
    model_sensitivity = CardValidationResult(
        check_id="bounded_scenario_sensitivity",
        status=ValidationStatus.PASS,
        summary=(
            "Joint constrained shocks and higher-order exposure outputs were "
            "recomputed by the public integrated profile."
        ),
        evidence_hash=bundle.integrated_profile_receipt.content_hash,
    )
    data_card = DataCard(
        card_id="card:data:builtin_public",
        version="v1",
        dataset_id="dataset:builtin_public_inputs",
        dataset_version=request.spec.data_version,
        source_name="Externally prepared immutable research input bundle",
        source_reference=(bundle.authoritative_input_receipt.artifact_logical_id),
        license_id="OFFLINE-RESEARCH-FIXTURE",
        license_terms_hash=evidence_hash(
            {
                "distribution": "legally-distributable-synthetic-fixture",
                "prohibited_use": "live_trading_or_market_data_inference",
            },
            label="builtin-public-data-license",
        ),
        distribution_status=DistributionStatus.REDISTRIBUTABLE,
        use_constraints=(
            "NO_LIVE_MARKET_OR_ACCOUNT_ACCESS",
            "OFFLINE_RESEARCH_ONLY",
        ),
        snapshot_method=(
            "Externally prepared immutable evidence envelopes are resolved "
            "twice by content hash before the decision cutoff."
        ),
        coverage_start_at=request.spec.data_range.start_at,
        coverage_end_at=request.spec.data_range.end_at,
        coverage_markets=tuple(sorted(request.spec.universe.asset_classes)),
        coverage_instruments=tuple(sorted(request.spec.universe.instrument_ids)),
        field_schema=(
            DataFieldDefinition(
                field_name="artifact_hash",
                data_type="sha256 string",
                semantic_type="source_artifact_hash",
                nullable=False,
                description="Hash of the immutable research-input artifact.",
            ),
            DataFieldDefinition(
                field_name="content_hash",
                data_type="sha256 string",
                semantic_type="canonical_record_hash",
                nullable=False,
                description="Hash of the complete authoritative source row.",
            ),
            DataFieldDefinition(
                field_name="decision_cutoff",
                data_type="RFC3339 timestamp",
                semantic_type="availability_cutoff",
                nullable=False,
                description="Latest knowledge time admitted to the study.",
            ),
            DataFieldDefinition(
                field_name="event_at",
                data_type="RFC3339 timestamp",
                semantic_type="valid_time",
                nullable=False,
                description="Economic event time represented by the row.",
            ),
            DataFieldDefinition(
                field_name="input_path",
                data_type="JSON Pointer",
                semantic_type="canonical_input_locator",
                nullable=False,
                description="Canonical request leaf established by the row.",
            ),
            DataFieldDefinition(
                field_name="knowledge_at",
                data_type="RFC3339 timestamp",
                semantic_type="knowledge_and_availability_time",
                nullable=False,
                description="Time the source row became usable by research.",
            ),
            DataFieldDefinition(
                field_name="normalized_value",
                data_type="canonical JSON value",
                semantic_type="typed_canonical_input_value",
                nullable=False,
                description=(
                    "Canonical typed input value produced from the source value."
                ),
            ),
            DataFieldDefinition(
                field_name="row_id",
                data_type="stable identifier",
                semantic_type="source_row_identity",
                nullable=False,
                description="Stable identity of the immutable source row.",
            ),
            DataFieldDefinition(
                field_name="source_id",
                data_type="stable identifier",
                semantic_type="source_provider_identity",
                nullable=False,
                description="Identity of the offline source that issued the row.",
            ),
            DataFieldDefinition(
                field_name="source_payload_path",
                data_type="JSON Pointer",
                semantic_type="source_value_locator",
                nullable=False,
                description="Exact location of the covered value in the source row.",
            ),
            DataFieldDefinition(
                field_name="source_row_hash",
                data_type="sha256 string",
                semantic_type="source_row_hash",
                nullable=False,
                description="Canonical row hash used by the evidence resolver.",
            ),
            DataFieldDefinition(
                field_name="source_schema_version",
                data_type="version identifier",
                semantic_type="source_schema_identity",
                nullable=False,
                description="Immutable schema version governing the source row.",
            ),
            DataFieldDefinition(
                field_name="source_value",
                data_type="canonical JSON value",
                semantic_type="source_observation_value",
                nullable=False,
                description="Exact immutable source-row value before normalization.",
            ),
            DataFieldDefinition(
                field_name="value_hash",
                data_type="sha256 string",
                semantic_type="source_and_normalized_value_binding",
                nullable=False,
                description="Shared hash proving source and normalized value equality.",
            ),
        ),
        units=(
            DataFieldUnit(field_name="artifact_hash", unit="SHA256"),
            DataFieldUnit(field_name="content_hash", unit="SHA256"),
            DataFieldUnit(field_name="decision_cutoff", unit="UTC_TIMESTAMP"),
            DataFieldUnit(field_name="event_at", unit="UTC_TIMESTAMP"),
            DataFieldUnit(field_name="input_path", unit="JSON_POINTER"),
            DataFieldUnit(field_name="knowledge_at", unit="UTC_TIMESTAMP"),
            DataFieldUnit(field_name="normalized_value", unit="FIELD_DEFINED"),
            DataFieldUnit(field_name="row_id", unit="IDENTIFIER"),
            DataFieldUnit(field_name="source_id", unit="IDENTIFIER"),
            DataFieldUnit(field_name="source_payload_path", unit="JSON_POINTER"),
            DataFieldUnit(field_name="source_row_hash", unit="SHA256"),
            DataFieldUnit(field_name="source_schema_version", unit="IDENTIFIER"),
            DataFieldUnit(field_name="source_value", unit="FIELD_DEFINED"),
            DataFieldUnit(field_name="value_hash", unit="SHA256"),
        ),
        temporal_semantics=DataTemporalSemantics(
            valid_time_field="event_at",
            valid_time_definition=(
                "The time at which the represented economic fact is valid."
            ),
            knowledge_time_field="knowledge_at",
            knowledge_time_definition=(
                "The time at which the provider knew the represented fact."
            ),
            availability_time_field="knowledge_at",
            availability_time_definition=(
                "The earliest timestamp at which this offline research run may "
                "consume the row."
            ),
            timezone="UTC",
            calendar_ids=("calendar:public-fixture",),
        ),
        normalization_transformations=(
            "CANONICAL_DECIMAL_TEXT",
            "PROVIDER_FIELD_TO_TYPED_CONTRACT",
            "UTC_TIMESTAMP_CANONICALIZATION",
        ),
        known_corrections=(),
        missing_data_summary=(
            "The admitted fixture has complete required fields; unsupported "
            "or missing fields are rejected."
        ),
        missing_data_policy="Fail closed without imputation or source probing.",
        survivorship_policy=(
            "Use point-in-time universe and issuer/listing revisions known at "
            "each decision time; never apply a current constituent set backward."
        ),
        corporate_action_policy=(
            "Apply only typed corporate-action events present in immutable "
            "evidence and preserve value-consistent cash and quantity changes."
        ),
        known_biases=(
            "CLOSED_SUPPORTED_PROFILE",
            "SYNTHETIC_FIXTURE_NOT_REAL_MARKET_CALIBRATION",
        ),
        known_limitations=(
            "CLOSED_FIXTURE_DOES_NOT_ESTABLISH_VENDOR_WIDE_COVERAGE",
            "SYNTHETIC_VALUES_ARE_NOT_EMPIRICAL_MARKET_CALIBRATION",
        ),
        intended_uses=(
            "DETERMINISTIC_MULTI_ASSET_RESEARCH_CONFORMANCE",
            "OFFLINE_REPRODUCIBILITY_VALIDATION",
        ),
        prohibited_uses=(
            "LIVE_ORDER_ROUTING",
            "REAL_ACCOUNT_OR_PORTFOLIO_OPERATION",
        ),
        revision_policy=(
            "Immutable snapshots; any correction requires a new version and "
            "content hash."
        ),
        source_hashes=tuple(
            sorted(
                {
                    bundle.authoritative_input_receipt.artifact_hash,
                    bundle.authoritative_input_receipt.input_document_hash,
                    bundle.authoritative_input_receipt.source_rows_hash,
                    *bundle.authoritative_input_receipt.source_row_hashes,
                }
            )
        ),
        row_resolver_metadata=DataRowResolverMetadata(
            resolver_id="resolver:builtin-public-input",
            resolver_version="v1",
            row_identity_fields=("input_path", "row_id"),
            source_artifact_hash_field="artifact_hash",
            source_row_hash_field="source_row_hash",
            resolution_policy=(
                "Resolve the exact JSON Pointer to its unique covered row, "
                "then verify artifact and row content hashes."
            ),
        ),
        validation_results=(data_validation,),
        quality_flags=(
            "IMMUTABLE_SOURCE_ROWS",
            "ROW_LEVEL_PROVENANCE",
        ),
    )
    profile_hashes = (
        bundle.spot_profile_receipt.content_hash,
        bundle.futures_profile_receipt.content_hash,
        bundle.option_profile_receipt.content_hash,
        bundle.integrated_profile_receipt.content_hash,
    )
    model_card = ModelCard(
        card_id="card:model:builtin_public",
        version="v1",
        model_id="model:builtin_public_profiles",
        model_version=BUILTIN_RUNNER_VERSION,
        model_name="Offline institutional multi-asset conformance profiles",
        model_family="DETERMINISTIC_MULTI_ASSET_RESEARCH_PIPELINE",
        implementation_hash=evidence_hash(
            {
                "runner_id": BUILTIN_RUNNER_ID,
                "runner_version": BUILTIN_RUNNER_VERSION,
                "profile_receipt_hashes": list(profile_hashes),
            },
            label="builtin-public-profile-implementation",
        ),
        code_hash=evidence_hash(
            {
                "runner_id": BUILTIN_RUNNER_ID,
                "runner_version": BUILTIN_RUNNER_VERSION,
            },
            label="builtin-public-runner-code",
        ),
        configuration_hash=request.spec.content_hash,
        input_schema_hash=evidence_hash(
            {
                "input_schema_id": (bundle.authoritative_input_receipt.input_schema_id),
                "input_schema_version": (
                    bundle.authoritative_input_receipt.input_schema_version
                ),
            },
            label="builtin-public-input-schema",
        ),
        output_schema_hash=evidence_hash(
            {
                "output_type": "PublicExecutionEvidenceBundle",
                "schema_version": 1,
            },
            label="builtin-public-output-schema",
        ),
        input_hashes=tuple(
            sorted(
                {
                    bundle.authoritative_input_receipt.artifact_hash,
                    bundle.authoritative_input_receipt.input_document_hash,
                    bundle.authoritative_input_receipt.source_rows_hash,
                }
            )
        ),
        output_hashes=tuple(sorted(profile_hashes)),
        assumptions=(
            "BOUNDED_CLOSED_REGISTRIES",
            "EXTERNALLY_PREPARED_IMMUTABLE_INPUTS",
            "NO_NETWORK_OR_ACCOUNT_STATE",
        ),
        applicability_scope=("SUPPORTED_MULTI_ASSET_CONFORMANCE_FIXTURES",),
        unsupported_cases=(
            "LIVE_OR_ACCOUNT_CONNECTED_EXECUTION",
            "UNKNOWN_PROVIDER_PRODUCT_OR_POLICY_SEMANTICS",
        ),
        parameters=(
            ModelParameter(
                parameter_name="profile_count",
                value="4",
                data_type="integer",
                unit="COUNT",
                description="Number of authoritative public economic profiles.",
            ),
            ModelParameter(
                parameter_name="runner_id",
                value=BUILTIN_RUNNER_ID,
                data_type="stable identifier",
                unit="IDENTIFIER",
                description="Closed source-owned runner selected by the request.",
            ),
            ModelParameter(
                parameter_name="runner_version",
                value=BUILTIN_RUNNER_VERSION,
                data_type="version identifier",
                unit="IDENTIFIER",
                description="Reviewed deterministic runner contract version.",
            ),
        ),
        calibration_data_hashes=tuple(
            sorted(
                {
                    bundle.authoritative_input_receipt.artifact_hash,
                    bundle.authoritative_input_receipt.source_rows_hash,
                }
            )
        ),
        calibration_process=(
            "Clean and repair the supplied option chain, calibrate only through "
            "the closed model registry, and bind every profile to the immutable "
            "input receipt."
        ),
        objective=(
            "Reproduce the supported T-01 through T-04 economic paths and "
            "reconcile their ledger, exposure, attribution, and scenario output."
        ),
        diagnostic_results=(model_diagnostic,),
        convergence_criteria=(
            "Both executions must produce exactly identical canonical hashes "
            "for every mandatory economic object."
        ),
        convergence_result=model_convergence,
        failure_conditions=(
            "FUTURE_KNOWLEDGE_OR_MISSING_SOURCE_COVERAGE",
            "UNKNOWN_PROVIDER_CONVENTION_OR_PRODUCT",
            "UNRECONCILED_LEDGER_EXPOSURE_OR_ATTRIBUTION",
        ),
        failure_behavior=(
            "Fail closed without publishing a successful study or substituting "
            "caller-supplied economics."
        ),
        validation_results=(model_validation,),
        benchmark_results=(model_benchmark,),
        sensitivity_results=(model_sensitivity,),
        deterministic_configuration=(
            ModelParameter(
                parameter_name="random_seed",
                value=str(request.spec.seed),
                data_type="integer",
                unit="SEED",
                description="Experiment seed bound into the study specification.",
            ),
            ModelParameter(
                parameter_name="repeat_count",
                value="2",
                data_type="integer",
                unit="COUNT",
                description="Independent in-process deterministic executions.",
            ),
        ),
        known_limitations=(
            "NOT_EXHAUSTIVE_EXCHANGE_PROVIDER_OR_TAX_COVERAGE",
            "NOT_PROPRIETARY_FEED_OR_REAL_ORDER_BOOK_CALIBRATION",
        ),
        quality_flags=(
            "DETERMINISTIC_PUBLIC_PROFILE",
            "SOURCE_BOUND_MODEL_OUTPUT",
        ),
    )
    profile_card_specs = (
        (
            "t01",
            "Spot lifecycle, normalization, and accounting profile",
            "DETERMINISTIC_SPOT_RESEARCH_PIPELINE",
            bundle.spot_profile_receipt.content_hash,
            (
                "Normalize two provider conventions, resolve point-in-time "
                "identity, apply corporate-action and borrow-recall lifecycle, "
                "then reconcile ledger, exposure, cost, stress, and attribution."
            ),
        ),
        (
            "t02",
            "Futures selection, margin, and delivery profile",
            "DETERMINISTIC_FUTURES_RESEARCH_PIPELINE",
            bundle.futures_profile_receipt.content_hash,
            (
                "Bind continuous-signal provenance to an actual contract, "
                "execute margin and settlement or delivery lifecycle, then "
                "reconcile the common ledger, exposure, and stress outputs."
            ),
        ),
        (
            "t03",
            "Option surface, model, and lifecycle profile",
            "DETERMINISTIC_OPTION_ANALYTICS_PIPELINE",
            bundle.option_profile_receipt.content_hash,
            (
                "Normalize and clean the option chain, repair and calibrate the "
                "surface, compute source-owned analytics through the closed "
                "model registry, and reconcile path lifecycle and attribution."
            ),
        ),
        (
            "t04",
            "Integrated constrained multi-leg lifecycle profile",
            "DETERMINISTIC_INTEGRATED_MULTI_LEG_PIPELINE",
            bundle.integrated_profile_receipt.content_hash,
            (
                "Jointly size source-selected legs under economic constraints, "
                "simulate partial execution and dynamic lifecycle actions, then "
                "reconcile multi-currency accounting, exposure, and path stress."
            ),
        ),
    )
    model_cards = tuple(
        replace(
            model_card,
            card_id=f"card:model:builtin_public:{profile_id}",
            model_id=f"model:builtin_public:{profile_id}",
            model_name=model_name,
            model_family=model_family,
            implementation_hash=evidence_hash(
                {
                    "runner_id": BUILTIN_RUNNER_ID,
                    "runner_version": BUILTIN_RUNNER_VERSION,
                    "profile_id": profile_id,
                    "profile_receipt_hash": profile_receipt_hash,
                },
                label="builtin-public-profile-implementation",
            ),
            output_schema_hash=evidence_hash(
                {
                    "output_type": f"Public{profile_id.upper()}ProfileReceipt",
                    "schema_version": 1,
                },
                label="builtin-public-profile-output-schema",
            ),
            output_hashes=(profile_receipt_hash,),
            parameters=(
                ModelParameter(
                    parameter_name="profile_id",
                    value=profile_id.upper(),
                    data_type="stable identifier",
                    unit="IDENTIFIER",
                    description="Authoritative public economic profile.",
                ),
                ModelParameter(
                    parameter_name="runner_id",
                    value=BUILTIN_RUNNER_ID,
                    data_type="stable identifier",
                    unit="IDENTIFIER",
                    description="Closed source-owned runner selected by the request.",
                ),
                ModelParameter(
                    parameter_name="runner_version",
                    value=BUILTIN_RUNNER_VERSION,
                    data_type="version identifier",
                    unit="IDENTIFIER",
                    description="Reviewed deterministic runner contract version.",
                ),
            ),
            objective=objective,
        )
        for (
            profile_id,
            model_name,
            model_family,
            profile_receipt_hash,
            objective,
        ) in profile_card_specs
    )
    return data_card, model_cards


def _build_builtin_portable_package(
    *,
    paths: ResearchPathManager,
    request: BuiltinMultiAssetRequest,
    execution: MultiAssetResearchExecution,
    bundle: PublicExecutionEvidenceBundle,
) -> str:
    data_card, model_cards = _builtin_public_cards(
        request=request,
        bundle=bundle,
    )
    runtime = execution.run_manifest.runtime
    engine_source_archive = build_portable_engine_source_archive(paths.project_root)
    expected_study = execution.study.as_dict()
    expected_report = validated_study_report_payload(execution.study)

    def portable_json_file_bytes(value: object) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

    resolver = BoundedEvidenceArtifactResolver.from_paths(paths)
    evidence_envelopes = []
    for reference in sorted(
        request.evidence_references,
        key=lambda item: (
            item.role.value,
            item.logical_id,
            item.version,
        ),
    ):
        raw = resolver.read_verified_bytes(reference)
        evidence_envelopes.append(
            {
                "role": reference.role.value,
                "logical_id": reference.logical_id,
                "version": reference.version,
                "content_hash": reference.content_hash,
                "schema_hash": reference.schema_hash,
                "byte_length": reference.byte_length,
                "payload_base64": base64.b64encode(raw).decode("ascii"),
            }
        )
    engine_replay_descriptor = {
        "schema_version": 1,
        "request": request.as_dict(),
        "evidence_envelopes": evidence_envelopes,
        "expected_study_content_hash": execution.study.content_hash,
        "expected_study_artifact_hash": bytes_sha256(
            portable_json_file_bytes(expected_study)
        ),
        "expected_report_artifact_hash": bytes_sha256(
            portable_json_file_bytes(expected_report)
        ),
        "expected_accounting_reconciliation_hash": (
            execution.study.accounting_reconciliation_hash
        ),
        "engine_source_content_hash": bytes_sha256(engine_source_archive),
    }
    authoritative_receipt_payload = bundle.authoritative_input_receipt.as_dict()
    source_rows_by_id = {
        row.row_id: row for row in bundle.authoritative_input_receipt.source_rows
    }
    resolver_rows = [
        {
            "artifact_hash": bundle.authoritative_input_receipt.artifact_hash,
            "content_hash": source_rows_by_id[item.source_row_id].content_hash,
            "decision_cutoff": (bundle.authoritative_input_receipt.decision_cutoff),
            "event_at": source_rows_by_id[item.source_row_id].event_at,
            "input_path": item.input_path,
            "knowledge_at": source_rows_by_id[item.source_row_id].knowledge_at,
            "normalized_value": (
                bundle.authoritative_input_receipt.input_value_for_path(item.input_path)
            ),
            "row_id": item.source_row_id,
            "source_id": source_rows_by_id[item.source_row_id].source_id,
            "source_payload_path": item.source_payload_path,
            "source_row_hash": item.source_row_hash,
            "source_schema_version": (
                source_rows_by_id[item.source_row_id].source_schema_version
            ),
            "source_value": (
                bundle.authoritative_input_receipt.source_value_for_path(
                    item.input_path
                )
            ),
            "value_hash": item.value_hash,
        }
        for item in bundle.authoritative_input_receipt.coverage
    ]
    normalization_records = [
        {
            "record_id": (
                "normalized:"
                + evidence_hash(
                    {"input_path": item["input_path"]},
                    label="builtin-public-normalized-record-id",
                ).removeprefix("sha256:")
            ),
            "input_path": item["input_path"],
            "normalized_value": item["normalized_value"],
            "normalized_value_hash": item["value_hash"],
            "source_artifact_hash": item["artifact_hash"],
            "source_payload_path": item["source_payload_path"],
            "source_row_hash": item["source_row_hash"],
            "source_row_id": item["row_id"],
            "source_value": item["source_value"],
            "transformation_id": "AUTHORITATIVE_INPUT_PATH_NORMALIZATION_V1",
            "transformation_policy_hash": evidence_hash(
                request.inputs.policy.as_dict(),
                label="builtin-public-normalization-policy",
            ),
        }
        for item in resolver_rows
    ]
    immutable_inputs_payload = {
        **authoritative_receipt_payload,
        "resolver_rows": resolver_rows,
    }
    build_request = build_public_validated_package_request(
        PublicPackageMaterials(
            package_id=f"{request.spec.experiment_id}.validated",
            package_version="v1",
            seed=request.spec.seed,
            request=request.as_dict(),
            specification=request.spec.as_dict(),
            immutable_inputs=immutable_inputs_payload,
            data_card=data_card,
            model_card=model_cards[0],
            additional_model_cards=model_cards[1:],
            policy=request.inputs.policy.as_dict(),
            configuration={
                "schema_version": 1,
                "profile": request.profile.value,
                "runner_id": BUILTIN_RUNNER_ID,
                "runner_version": BUILTIN_RUNNER_VERSION,
                "request_hash": request.content_hash,
                "scenario_ids": ["T-01", "T-02", "T-03", "T-04", "T-05"],
            },
            dependency_identity={
                "schema_version": 1,
                "dependency_versions": list(runtime.dependency_versions),
                "dependency_set_hash": evidence_hash(
                    list(runtime.dependency_versions),
                    label="builtin-public-dependency-set",
                ),
            },
            runtime_identity={
                "schema_version": 1,
                "network_required": False,
                "repository_required_for_cold_replay": False,
                "runtime": runtime.as_dict(),
            },
            normalized_evidence={
                "schema_version": 1,
                "records": normalization_records,
                "source_rows_hash": (
                    bundle.authoritative_input_receipt.source_rows_hash
                ),
                "coverage_hash": (bundle.authoritative_input_receipt.coverage_hash),
                "spot_normalization_receipt_hashes": list(
                    bundle.spot_profile_receipt.normalization_receipt_hashes
                ),
                "spot_normalized_record_hashes": list(
                    bundle.spot_profile_receipt.normalized_record_hashes
                ),
                "option_raw_provider_row_hashes": list(
                    bundle.option_profile_receipt.raw_provider_row_hashes
                ),
                "option_normalized_quote_hashes": list(
                    bundle.option_profile_receipt.normalized_quote_hashes
                ),
                "option_forward_receipt_hashes": list(
                    bundle.option_profile_receipt.forward_receipt_hashes
                ),
            },
            derived_evidence=bundle.as_dict(),
            accounting={
                "schema_version": 1,
                "study_content_hash": execution.study.content_hash,
                "accounting_evidence": (execution.study.accounting_evidence_payload()),
                "scenario_evidence": [
                    item.as_dict() for item in execution.study.scenarios
                ],
            },
            evidence_quality_flags=(
                "IMMUTABLE_SOURCE_ROWS",
                "PUBLIC_PROFILE_EXECUTED",
                "ROW_LEVEL_PROVENANCE",
            ),
            engine_source_archive=engine_source_archive,
            engine_replay_descriptor=engine_replay_descriptor,
            expected_study=expected_study,
            expected_report=expected_report,
        )
    )
    package_parent = paths.research_artifact_path(
        request.spec.experiment_id,
        "portable-packages",
    )
    package_parent.mkdir(parents=True, exist_ok=True)
    target = (package_parent / request.content_hash.removeprefix("sha256:")).resolve()
    published = build_validated_package(
        build_request,
        target,
        project_root=paths.project_root,
    )
    return published.manifest.content_hash


def execute_builtin_multi_asset(
    *,
    paths: ResearchPathManager,
    request_path: str | Path,
    output_path: str | Path,
    command: Sequence[str],
) -> BuiltinExecutionRecord:
    request = load_builtin_multi_asset_request(paths, request_path)
    application_request = request.to_application_request(
        paths=paths,
        command=command,
    )
    execution = MultiAssetResearchApplicationService().execute(application_request)
    runner = application_request.runners.option
    if not isinstance(runner, _AuthoritativeBuiltinRunner):
        raise MultiAssetExperimentError("builtin_option_runner_authority_mismatch")
    (
        eligible_contract_ids,
        option_selection_hash,
        option_cleaned_chain_hash,
    ) = runner.option_selection_evidence(1)
    first_public_evidence = runner.public_execution_evidence(1)
    repeated_public_evidence = runner.public_execution_evidence(2)
    if first_public_evidence.content_hash != repeated_public_evidence.content_hash:
        raise MultiAssetExperimentError(
            "builtin_public_execution_evidence_repeat_mismatch"
        )
    (
        _public_evidence_path,
        public_evidence_hash,
        public_evidence_checksum,
    ) = publish_public_execution_evidence_bundle(
        path_manager=paths,
        experiment_id=request.spec.experiment_id,
        bundle=first_public_evidence,
    )
    portable_package_manifest_hash = _build_builtin_portable_package(
        paths=paths,
        request=request,
        execution=execution,
        bundle=first_public_evidence,
    )
    record = BuiltinExecutionRecord(
        request_hash=request.content_hash,
        run_id=request.run_id,
        experiment_spec_hash=request.spec.content_hash,
        study_content_hash=execution.study.content_hash,
        run_manifest_hash=execution.run_manifest.content_hash,
        study_artifact_hash=ArtifactChecksum.from_path(
            "multi_asset_study",
            execution.published_study.artifact_path,
        ).content_hash,
        report_artifact_hash=ArtifactChecksum.from_path(
            "validated_study_report",
            execution.published_study.report_path,
        ).content_hash,
        manifest_artifact_hash=ArtifactChecksum.from_path(
            "multi_asset_run_manifest",
            execution.published_manifest.path,
        ).content_hash,
        option_eligible_contract_ids=eligible_contract_ids,
        option_selection_hash=option_selection_hash,
        option_cleaned_chain_hash=option_cleaned_chain_hash,
        public_profile_receipt_hashes=(
            first_public_evidence.spot_profile_receipt.content_hash,
            first_public_evidence.futures_profile_receipt.content_hash,
            first_public_evidence.option_profile_receipt.content_hash,
            first_public_evidence.integrated_profile_receipt.content_hash,
        ),
        public_execution_evidence_hash=public_evidence_hash,
        public_execution_evidence_artifact_hash=(public_evidence_checksum.content_hash),
        portable_package_manifest_hash=portable_package_manifest_hash,
    )
    write_external_derivative_json(
        output_path,
        paths,
        record.as_dict(),
        "multi_asset_builtin_execution",
    )
    return record


def reproduce_builtin_multi_asset(
    *,
    paths: ResearchPathManager,
    request_path: str | Path,
    expected_path: str | Path,
    reproduction_run_id: str,
    output_path: str | Path,
    command: Sequence[str],
) -> BuiltinReproductionRecord:
    request = load_builtin_multi_asset_request(paths, request_path)
    expected = load_builtin_execution_record(paths, expected_path)
    if expected.request_hash != request.content_hash:
        raise BuiltinMultiAssetCodecError(
            "builtin_reproduction_expected_request_mismatch"
        )
    if expected.experiment_spec_hash != request.spec.content_hash:
        raise BuiltinMultiAssetCodecError("builtin_reproduction_expected_spec_mismatch")
    if reproduction_run_id == request.run_id:
        raise BuiltinMultiAssetCodecError("builtin_reproduction_run_id_must_differ")
    _require_id(
        reproduction_run_id,
        "builtin_reproduction.reproduction_run_id",
    )
    application_request = request.to_application_request(
        paths=paths,
        command=command,
        run_id=reproduction_run_id,
    )
    execution = MultiAssetResearchApplicationService().execute(application_request)
    runner = application_request.runners.option
    if not isinstance(runner, _AuthoritativeBuiltinRunner):
        raise MultiAssetExperimentError("builtin_option_runner_authority_mismatch")
    (
        reproduced_eligible_contract_ids,
        reproduced_selection_hash,
        reproduced_cleaned_chain_hash,
    ) = runner.option_selection_evidence(1)
    reproduced_public_evidence = runner.public_execution_evidence(1)
    repeated_public_evidence = runner.public_execution_evidence(2)
    if reproduced_public_evidence.content_hash != repeated_public_evidence.content_hash:
        raise MultiAssetExperimentError(
            "builtin_public_execution_evidence_repeat_mismatch"
        )
    (
        _reproduced_public_evidence_path,
        reproduced_public_evidence_hash,
        reproduced_public_evidence_checksum,
    ) = publish_public_execution_evidence_bundle(
        path_manager=paths,
        experiment_id=request.spec.experiment_id,
        bundle=reproduced_public_evidence,
    )
    reproduced_portable_package_manifest_hash = _build_builtin_portable_package(
        paths=paths,
        request=request,
        execution=execution,
        bundle=reproduced_public_evidence,
    )
    reproduced_study_artifact_hash = ArtifactChecksum.from_path(
        "multi_asset_study",
        execution.published_study.artifact_path,
    ).content_hash
    mismatches: list[str] = []
    if execution.study.content_hash != expected.study_content_hash:
        mismatches.append("study_content_hash")
    if reproduced_study_artifact_hash != expected.study_artifact_hash:
        mismatches.append("study_artifact_hash")
    if reproduced_eligible_contract_ids != expected.option_eligible_contract_ids:
        mismatches.append("option_eligible_contract_ids")
    if reproduced_selection_hash != expected.option_selection_hash:
        mismatches.append("option_selection_hash")
    if reproduced_cleaned_chain_hash != expected.option_cleaned_chain_hash:
        mismatches.append("option_cleaned_chain_hash")
    reproduced_profile_hashes = (
        reproduced_public_evidence.spot_profile_receipt.content_hash,
        reproduced_public_evidence.futures_profile_receipt.content_hash,
        reproduced_public_evidence.option_profile_receipt.content_hash,
        reproduced_public_evidence.integrated_profile_receipt.content_hash,
    )
    if reproduced_profile_hashes != expected.public_profile_receipt_hashes:
        mismatches.append("public_profile_receipt_hashes")
    if reproduced_public_evidence_hash != expected.public_execution_evidence_hash:
        mismatches.append("public_execution_evidence_hash")
    if (
        reproduced_public_evidence_checksum.content_hash
        != expected.public_execution_evidence_artifact_hash
    ):
        mismatches.append("public_execution_evidence_artifact_hash")
    if (
        reproduced_portable_package_manifest_hash
        != expected.portable_package_manifest_hash
    ):
        mismatches.append("portable_package_manifest_hash")
    record = BuiltinReproductionRecord(
        request_hash=request.content_hash,
        expected_execution_hash=expected.content_hash,
        reproduction_run_id=reproduction_run_id,
        expected_study_hash=expected.study_content_hash,
        reproduced_study_hash=execution.study.content_hash,
        reproduced_manifest_hash=execution.run_manifest.content_hash,
        expected_public_execution_evidence_hash=(
            expected.public_execution_evidence_hash
        ),
        reproduced_public_execution_evidence_hash=(reproduced_public_evidence_hash),
        reproduced_public_execution_evidence_artifact_hash=(
            reproduced_public_evidence_checksum.content_hash
        ),
        expected_portable_package_manifest_hash=(
            expected.portable_package_manifest_hash
        ),
        reproduced_portable_package_manifest_hash=(
            reproduced_portable_package_manifest_hash
        ),
        status=(
            BuiltinReproductionStatus.PASS
            if not mismatches
            else BuiltinReproductionStatus.FAIL
        ),
        mismatch_fields=tuple(sorted(mismatches)),
    )
    write_external_derivative_json(
        output_path,
        paths,
        record.as_dict(),
        "multi_asset_builtin_reproduction",
    )
    return record


__all__ = [
    "BUILTIN_MULTI_ASSET_SCHEMA_VERSION",
    "BUILTIN_RUNNER_ID",
    "BUILTIN_RUNNER_VERSION",
    "BuiltinEconomicScenarioPolicy",
    "BuiltinExecutionRecord",
    "BuiltinMultiAssetCodecError",
    "BuiltinMultiAssetRequest",
    "BuiltinReproductionRecord",
    "BuiltinReproductionStatus",
    "BuiltinRunnerProfile",
    "BuiltinScenarioInputs",
    "BuiltinSpotScenarioInput",
    "execute_builtin_multi_asset",
    "load_builtin_execution_record",
    "load_builtin_multi_asset_request",
    "reproduce_builtin_multi_asset",
    "write_builtin_multi_asset_request",
]
