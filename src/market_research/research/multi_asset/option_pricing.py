"""Hash-bound common option pricing backed by the legacy vanilla engine.

The common pricing protocol deliberately accepts opaque contract and market-state
objects.  This adapter narrows that seam at runtime to the immutable Research
Semantics v2 option contracts already used by the derivative research engine.
Prices and Greeks are always recomputed by :class:`BlackScholesModel`; callers
cannot inject precomputed analytics through ``OptionPricingState``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from market_research.research.derivatives.common import (
    DERIVATIVE_RESEARCH_SCHEMA_VERSION,
    DerivativeResearchError,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from market_research.research.derivatives.options import (
    BlackScholesModel,
    ExerciseStyle,
    OptionContract,
    OptionType,
    QuoteState,
    ValuationInputSnapshot,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.market_state import (
    OptionAnalyticsMark,
    OptionContractQuote,
    OptionRight as MarketStateOptionRight,
    QuoteCondition,
)
from market_research.research.multi_asset.option_path import (
    OptionGreeks,
    PricingModelSpecification,
)


_CALENDAR_DAYS_PER_YEAR = Decimal("365.25")
_ONE_PERCENT = Decimal("0.01")
_MODEL_ID = "black_scholes_european"
_DAY_COUNT = "ACT/365.25"
_RATE_CURVE_ID = "valuation_input.risk_free_rate"
_DIVIDEND_MODEL = "continuous_dividend_yield"
_DISCRETE_DIVIDEND_POLICY = "unsupported_fail_closed"
_BORROW_POLICY = "not_separately_modelled"
_NUMERICAL_METHOD = "closed_form_price_greeks_bisection_iv"
_CONVERGENCE_POLICY = "model_bounds_tolerance_and_iteration_limit"


class OptionPricingAdapterError(DerivativeResearchError):
    """Raised when a common option-pricing request is not exactly bound."""


def black_scholes_pricing_specification(
    model_version: str = "black_scholes_european_v1",
) -> PricingModelSpecification:
    """Return the canonical assumptions for the legacy Black-Scholes model."""

    return PricingModelSpecification(
        model_id=_MODEL_ID,
        implementation_version=model_version,
        exercise_styles=(ExerciseStyle.EUROPEAN.value,),
        day_count=_DAY_COUNT,
        rate_curve_id=_RATE_CURVE_ID,
        dividend_model=_DIVIDEND_MODEL,
        discrete_dividend_policy=_DISCRETE_DIVIDEND_POLICY,
        borrow_policy=_BORROW_POLICY,
        numerical_method=_NUMERICAL_METHOD,
        convergence_policy=_CONVERGENCE_POLICY,
    )


@dataclass(frozen=True, slots=True)
class OptionPricingState:
    """Immutable valuation inputs, volatility, and their model hash bindings."""

    valuation_input: ValuationInputSnapshot
    volatility: Decimal
    valuation_input_hash: str
    contract_hash: str
    pricing_model_hash: str
    specification_hash: str
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise OptionPricingAdapterError(
                "option_pricing_state_schema_version_unsupported"
            )
        if not isinstance(self.valuation_input, ValuationInputSnapshot):
            raise OptionPricingAdapterError(
                "option_pricing_state_valuation_input_required"
            )
        volatility = exact_decimal(
            self.volatility,
            "option_pricing_state.volatility",
            positive=True,
        )
        object.__setattr__(self, "volatility", volatility)
        for field_name in (
            "valuation_input_hash",
            "contract_hash",
            "pricing_model_hash",
            "specification_hash",
        ):
            require_hash(
                str(getattr(self, field_name)),
                f"option_pricing_state.{field_name}",
            )
        if self.valuation_input_hash != self.valuation_input.content_hash:
            raise OptionPricingAdapterError(
                "option_pricing_state_valuation_input_hash_mismatch"
            )
        if self.contract_hash != self.valuation_input.contract.content_hash:
            raise OptionPricingAdapterError(
                "option_pricing_state_contract_hash_mismatch"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="option_pricing_state",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "valuation_input": self.valuation_input.as_dict(),
            "valuation_input_hash": self.valuation_input_hash,
            "contract_hash": self.contract_hash,
            "volatility": decimal_text(self.volatility),
            "pricing_model_hash": self.pricing_model_hash,
            "specification_hash": self.specification_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class BlackScholesPricingAdapter:
    """Concrete ``CommonOptionPricingModel`` for European vanilla options.

    ``value`` is quote-currency cash per underlying unit.  Delta and gamma keep
    the legacy per-underlying-unit convention.  Vega and rho are converted from
    a unit bump to one percentage point, and theta is converted from annual to
    one ACT/365.25 calendar day.  Contract multiplier application remains a
    portfolio/exposure concern rather than a pricing concern.
    """

    model: BlackScholesModel = field(default_factory=BlackScholesModel)
    specification: PricingModelSpecification = field(
        default_factory=black_scholes_pricing_specification
    )
    adapter_version: str = "black_scholes_pricing_adapter_v1"
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise OptionPricingAdapterError(
                "option_pricing_adapter_schema_version_unsupported"
            )
        if not isinstance(self.model, BlackScholesModel):
            raise OptionPricingAdapterError("black_scholes_model_required")
        if not isinstance(self.specification, PricingModelSpecification):
            raise OptionPricingAdapterError(
                "option_pricing_model_specification_required"
            )
        require_stable_id(
            self.adapter_version,
            "option_pricing_adapter.adapter_version",
        )
        if self.specification.implementation_version != self.model.model_version:
            raise OptionPricingAdapterError("option_pricing_model_version_mismatch")
        expected_assumptions: dict[str, object] = {
            "model_id": _MODEL_ID,
            "exercise_styles": (ExerciseStyle.EUROPEAN.value,),
            "day_count": _DAY_COUNT,
            "rate_curve_id": _RATE_CURVE_ID,
            "dividend_model": _DIVIDEND_MODEL,
            "discrete_dividend_policy": _DISCRETE_DIVIDEND_POLICY,
            "borrow_policy": _BORROW_POLICY,
            "numerical_method": _NUMERICAL_METHOD,
            "convergence_policy": _CONVERGENCE_POLICY,
        }
        for field_name, expected in expected_assumptions.items():
            if getattr(self.specification, field_name) != expected:
                raise OptionPricingAdapterError(
                    f"option_pricing_assumption_mismatch:{field_name}"
                )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="black_scholes_pricing_adapter",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "adapter_version": self.adapter_version,
            "model": self.model.as_dict(),
            "model_hash": self.model.content_hash,
            "specification": {
                **asdict(self.specification),
                "content_hash": self.specification.content_hash,
            },
            "specification_hash": self.specification.content_hash,
            "value_convention": "quote_currency_per_underlying_unit",
            "vega_convention": "quote_currency_per_one_volatility_point_per_unit",
            "theta_convention": "quote_currency_per_calendar_day_per_unit",
            "rho_convention": "quote_currency_per_one_rate_point_per_unit",
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def bind_state(
        self,
        valuation_input: ValuationInputSnapshot,
        volatility: Decimal,
    ) -> OptionPricingState:
        """Bind an immutable input snapshot to this exact model and spec."""

        if not isinstance(valuation_input, ValuationInputSnapshot):
            raise OptionPricingAdapterError(
                "option_pricing_state_valuation_input_required"
            )
        state = OptionPricingState(
            valuation_input=valuation_input,
            volatility=volatility,
            valuation_input_hash=valuation_input.content_hash,
            contract_hash=valuation_input.contract.content_hash,
            pricing_model_hash=self.model.content_hash,
            specification_hash=self.specification.content_hash,
        )
        self._validated(valuation_input.contract, state)
        return state

    def value(self, contract: object, market_state: object) -> Decimal:
        inputs, state = self._validated(contract, market_state)
        try:
            return self.model.price(inputs, state.volatility)
        except DerivativeResearchError as exc:
            raise OptionPricingAdapterError(f"option_value_failed:{exc}") from exc

    def greeks(self, contract: object, market_state: object) -> OptionGreeks:
        inputs, state = self._validated(contract, market_state)
        try:
            legacy = self.model.greeks(inputs, state.volatility)
        except DerivativeResearchError as exc:
            raise OptionPricingAdapterError(f"option_greeks_failed:{exc}") from exc
        if (
            legacy.contract_id != inputs.contract.contract_id
            or legacy.valuation_input_hash != state.valuation_input_hash
            or legacy.model_version != self.model.model_version
        ):
            raise OptionPricingAdapterError("option_greeks_result_binding_mismatch")
        return OptionGreeks(
            delta=legacy.delta,
            gamma=legacy.gamma,
            vega_per_vol_point=legacy.vega * _ONE_PERCENT,
            theta_per_calendar_day=legacy.theta_per_year / _CALENDAR_DAYS_PER_YEAR,
            rho_per_rate_point=legacy.rho * _ONE_PERCENT,
        )

    def implied_parameter(
        self,
        contract: object,
        observed_price: Decimal,
        market_state: object,
    ) -> Decimal:
        inputs, state = self._validated(contract, market_state)
        selected_price = exact_decimal(
            observed_price,
            "option_pricing_adapter.observed_price",
            positive=True,
        )
        result = self.model.implied_volatility(inputs, selected_price)
        if (
            result.contract_id != inputs.contract.contract_id
            or result.valuation_input_hash != state.valuation_input_hash
            or result.model_version != self.model.model_version
        ):
            raise OptionPricingAdapterError("option_iv_result_binding_mismatch")
        if not result.success or result.volatility is None:
            raise OptionPricingAdapterError(
                f"option_implied_volatility_failed:{result.failure.value}"
            )
        return result.volatility

    def scenario_value(
        self,
        contract: object,
        shocked_market_state: object,
    ) -> Decimal:
        inputs, state = self._validated(contract, shocked_market_state)
        try:
            return self.model.price(inputs, state.volatility)
        except DerivativeResearchError as exc:
            raise OptionPricingAdapterError(
                f"option_scenario_value_failed:{exc}"
            ) from exc

    def _validated(
        self,
        contract: object,
        market_state: object,
    ) -> tuple[ValuationInputSnapshot, OptionPricingState]:
        if not isinstance(contract, OptionContract):
            raise OptionPricingAdapterError("option_contract_required")
        if not isinstance(market_state, OptionPricingState):
            raise OptionPricingAdapterError("option_pricing_state_required")
        inputs = market_state.valuation_input
        if (
            contract.content_hash != market_state.contract_hash
            or inputs.contract.content_hash != contract.content_hash
        ):
            raise OptionPricingAdapterError("option_contract_input_mismatch")
        if inputs.content_hash != market_state.valuation_input_hash:
            raise OptionPricingAdapterError("option_valuation_input_binding_mismatch")
        if market_state.pricing_model_hash != self.model.content_hash:
            raise OptionPricingAdapterError("option_pricing_model_hash_mismatch")
        if market_state.specification_hash != self.specification.content_hash:
            raise OptionPricingAdapterError(
                "option_pricing_specification_hash_mismatch"
            )
        if inputs.contract.exercise_style is not ExerciseStyle.EUROPEAN:
            raise OptionPricingAdapterError("black_scholes_requires_european_option")
        if inputs.time_to_expiry_years <= 0:
            raise OptionPricingAdapterError("option_pricing_contract_expired")
        if not (
            self.model.minimum_volatility
            <= market_state.volatility
            <= self.model.maximum_volatility
        ):
            raise OptionPricingAdapterError("option_pricing_volatility_out_of_bounds")
        return inputs, market_state


@dataclass(frozen=True, slots=True)
class BlackScholesOptionAnalyticsFactory:
    """Derive a typed analytics mark without accepting caller-supplied analytics.

    The typed quote is bound to the legacy valuation snapshot through contract,
    quote fields, availability clocks, and the legacy quote content hash.  Price,
    implied volatility, and every Greek are then recomputed by the configured
    pricing adapter.  Margin amounts remain explicit external assumptions, while
    their model identity is fixed in the factory itself.
    """

    margin_model_hash: str
    pricing_adapter: BlackScholesPricingAdapter = field(
        default_factory=BlackScholesPricingAdapter
    )
    factory_version: str = "black_scholes_option_analytics_factory_v1"
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise OptionPricingAdapterError(
                "option_analytics_factory_schema_version_unsupported"
            )
        if not isinstance(self.pricing_adapter, BlackScholesPricingAdapter):
            raise OptionPricingAdapterError(
                "option_analytics_factory_pricing_adapter_required"
            )
        require_hash(
            self.margin_model_hash,
            "option_analytics_factory.margin_model_hash",
        )
        require_stable_id(
            self.factory_version,
            "option_analytics_factory.factory_version",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="black_scholes_option_analytics_factory",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "factory_version": self.factory_version,
            "pricing_adapter_hash": self.pricing_adapter.content_hash,
            "pricing_model_hash": self.pricing_adapter.model.content_hash,
            "model_specification_hash": (
                self.pricing_adapter.specification.content_hash
            ),
            "margin_model_hash": self.margin_model_hash,
            "market_price_policy": "typed_quote_bid_ask_midpoint",
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def derive(
        self,
        *,
        quote: OptionContractQuote,
        valuation_input: ValuationInputSnapshot,
        margin_per_contract: Decimal,
        collateral_per_contract: Decimal,
    ) -> OptionAnalyticsMark:
        """Recompute IV, model value, and Greeks for one exactly bound quote."""

        if not isinstance(quote, OptionContractQuote):
            raise OptionPricingAdapterError(
                "option_analytics_factory_typed_quote_required"
            )
        if not isinstance(valuation_input, ValuationInputSnapshot):
            raise OptionPricingAdapterError(
                "option_analytics_factory_valuation_input_required"
            )
        self._validate_quote_binding(quote, valuation_input)
        margin = exact_decimal(
            margin_per_contract,
            "option_analytics_factory.margin_per_contract",
        )
        collateral = exact_decimal(
            collateral_per_contract,
            "option_analytics_factory.collateral_per_contract",
        )
        if margin < 0 or collateral < 0:
            raise OptionPricingAdapterError(
                "option_analytics_factory_margin_or_collateral_negative"
            )

        model = self.pricing_adapter.model
        solver_seed = (model.minimum_volatility + model.maximum_volatility) / Decimal(
            "2"
        )
        seed_state = self.pricing_adapter.bind_state(valuation_input, solver_seed)
        market_price = quote.midpoint
        implied_volatility = self.pricing_adapter.implied_parameter(
            valuation_input.contract,
            market_price,
            seed_state,
        )
        priced_state = self.pricing_adapter.bind_state(
            valuation_input,
            implied_volatility,
        )
        model_price = self.pricing_adapter.value(
            valuation_input.contract,
            priced_state,
        )
        greeks = self.pricing_adapter.greeks(
            valuation_input.contract,
            priced_state,
        )
        if abs(model_price - market_price) > model.price_tolerance:
            raise OptionPricingAdapterError(
                "option_analytics_factory_repricing_tolerance_exceeded"
            )
        return OptionAnalyticsMark(
            contract_id=quote.contract_id,
            underlying_instrument_id=quote.underlying_instrument_id,
            expiry_at=quote.expiry_at,
            currency=quote.currency,
            price_unit=quote.price_unit,
            market_price=market_price,
            model_price=model_price,
            implied_volatility=implied_volatility,
            delta=greeks.delta,
            gamma=greeks.gamma,
            vega=greeks.vega_per_vol_point,
            theta=greeks.theta_per_calendar_day,
            rho=greeks.rho_per_rate_point,
            margin_per_contract=margin,
            collateral_per_contract=collateral,
            model_hash=self.pricing_adapter.model.content_hash,
            model_specification_hash=(self.pricing_adapter.specification.content_hash),
            margin_model_hash=self.margin_model_hash,
            valuation_input_hash=valuation_input.content_hash,
            source_quote_hash=quote.content_hash,
            metadata=quote.metadata,
        )

    @staticmethod
    def _validate_quote_binding(
        quote: OptionContractQuote,
        valuation_input: ValuationInputSnapshot,
    ) -> None:
        contract = valuation_input.contract
        source_quote = valuation_input.quote
        expected_right = (
            MarketStateOptionRight.CALL
            if contract.option_type is OptionType.CALL
            else MarketStateOptionRight.PUT
        )
        if (
            quote.contract_id != contract.contract_id
            or quote.underlying_instrument_id != contract.underlying_id
            or quote.right is not expected_right
            or quote.strike != contract.strike
            or quote.currency != contract.currency
            or parse_timestamp(quote.expiry_at, "option_quote.expiry_at")
            != parse_timestamp(contract.expiration_at, "option_contract.expiration_at")
        ):
            raise OptionPricingAdapterError(
                "option_analytics_factory_contract_binding_mismatch"
            )
        if source_quote.state is not QuoteState.NORMAL:
            raise OptionPricingAdapterError(
                "option_analytics_factory_source_quote_not_normal"
            )
        if quote.condition is not QuoteCondition.NORMAL:
            raise OptionPricingAdapterError(
                "option_analytics_factory_typed_quote_not_normal"
            )
        source_values = (
            source_quote.bid,
            source_quote.ask,
            source_quote.last,
            source_quote.bid_size,
            source_quote.ask_size,
            Decimal(source_quote.volume),
            Decimal(source_quote.open_interest),
        )
        typed_values = (
            quote.bid,
            quote.ask,
            quote.last,
            quote.bid_size,
            quote.ask_size,
            quote.volume,
            quote.open_interest,
        )
        if source_values != typed_values:
            raise OptionPricingAdapterError(
                "option_analytics_factory_quote_value_mismatch"
            )
        if quote.metadata.source_hash != source_quote.content_hash:
            raise OptionPricingAdapterError(
                "option_analytics_factory_source_quote_hash_unbound"
            )
        if (
            parse_timestamp(
                quote.metadata.observed_at,
                "option_quote.metadata.observed_at",
            )
            != parse_timestamp(
                source_quote.availability.event_at,
                "option_quote.availability.event_at",
            )
            or parse_timestamp(
                quote.metadata.knowledge_at,
                "option_quote.metadata.knowledge_at",
            )
            != source_quote.availability.available_at
            or quote.metadata.max_age_seconds != source_quote.stale_after_seconds
        ):
            raise OptionPricingAdapterError(
                "option_analytics_factory_quote_availability_mismatch"
            )


__all__ = (
    "BlackScholesOptionAnalyticsFactory",
    "BlackScholesPricingAdapter",
    "OptionPricingAdapterError",
    "OptionPricingState",
    "black_scholes_pricing_specification",
)
