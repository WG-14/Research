from __future__ import annotations

from dataclasses import dataclass

from bithumb_research.execution_service import (
    ExecutionDecisionSummary,
    ExecutionSubmitPlan,
    SignalExecutionRequest,
    validate_execution_submit_plan_payload,
)

from .execution_model import ExecutionFill, ExecutionModel, ExecutionRequest


@dataclass(frozen=True)
class ResearchExecutionContext:
    signal_ts: int
    decision_ts: int
    timing_fields: dict[str, object]
    depth_fields: dict[str, object]

    def execution_request_fields(self) -> dict[str, object]:
        fields = dict(self.timing_fields)
        fields.update(dict(self.depth_fields))
        return fields


def execution_submit_plan_to_research_request(
    *,
    submit_plan: ExecutionSubmitPlan,
    context: ResearchExecutionContext,
    reference_price: float,
    fee_rate: float,
) -> ExecutionRequest | None:
    if not isinstance(submit_plan, ExecutionSubmitPlan):
        raise ValueError("research_submit_plan_not_typed")
    if not isinstance(context, ResearchExecutionContext):
        raise ValueError("research_execution_context_not_typed")
    payload = submit_plan.as_dict()
    validate_execution_submit_plan_payload(payload, field_name="research_submit_plan")
    if not bool(submit_plan.submit_expected):
        return None
    if str(submit_plan.block_reason or "none") not in {
        "none",
        "residual_buy_sizing_mode_telemetry",
    }:
        return None
    side = str(submit_plan.side or "").upper()
    if side == "BUY":
        requested_notional = _positive_float_or_none(submit_plan.notional_krw)
        if requested_notional is None:
            raise ValueError("research_buy_submit_plan_missing_size")
        if submit_plan.qty is None:
            if reference_price <= 0.0:
                raise ValueError("research_buy_submit_plan_missing_size")
            requested_qty = requested_notional / float(reference_price)
        else:
            requested_qty = _positive_float_or_none(submit_plan.qty)
            if requested_qty is None:
                raise ValueError("research_buy_submit_plan_missing_size")
    elif side == "SELL":
        requested_qty = _positive_float_or_none(submit_plan.qty)
        requested_notional = _positive_float_or_none(submit_plan.notional_krw)
        if requested_qty is None:
            raise ValueError("research_sell_submit_plan_missing_qty")
        if requested_notional is None:
            raise ValueError("research_sell_submit_plan_missing_notional")
    else:
        raise ValueError(f"research_submit_plan_unsupported_side:{side or 'missing'}")
    return ExecutionRequest(
        signal_ts=int(context.signal_ts),
        decision_ts=int(context.decision_ts),
        side=side,
        reference_price=float(reference_price),
        requested_qty=requested_qty,
        requested_notional=requested_notional,
        fee_rate=float(fee_rate),
        entry_signal_source=str(payload.get("entry_signal_source") or "") or None,
        entry_sizing_source=str(payload.get("entry_sizing_source") or "") or None,
        **context.execution_request_fields(),
    )


@dataclass(frozen=True)
class ResearchVirtualExecutionService:
    """Research execution adapter whose public boundary is SignalExecutionRequest."""

    execution_model: ExecutionModel
    fee_rate: float

    def execute(
        self,
        request: SignalExecutionRequest,
    ) -> ExecutionFill | None:
        if not isinstance(request, SignalExecutionRequest):
            raise ValueError("research_signal_execution_request_not_typed")
        context = request.research_execution_context
        if not isinstance(context, ResearchExecutionContext):
            raise ValueError("research_execution_context_not_typed")
        submit_plan = self._typed_submit_plan_from_request(request)
        if submit_plan is None:
            raise ValueError("research_missing_typed_submit_plan")
        return self.simulate_submit_plan(
            submit_plan=submit_plan,
            context=context,
            reference_price=float(request.market_price),
        )

    def _typed_submit_plan_from_request(
        self,
        request: SignalExecutionRequest,
    ) -> ExecutionSubmitPlan | None:
        bundle = request.execution_plan_bundle
        bundle_plan = getattr(bundle, "submit_plan", None) if bundle is not None else None
        if bundle is not None and bundle_plan is not None and not isinstance(bundle_plan, ExecutionSubmitPlan):
            raise ValueError("research_dict_only_submit_plan_not_authority")
        if isinstance(bundle_plan, ExecutionSubmitPlan):
            return bundle_plan
        summary = request.execution_decision_summary or getattr(bundle, "summary", None)
        if summary is None:
            return None
        if not isinstance(summary, ExecutionDecisionSummary):
            raise ValueError("research_execution_summary_not_typed")
        for field_name, candidate in (
            ("target_submit_plan", summary.target_submit_plan),
            ("residual_submit_plan", summary.residual_submit_plan),
            ("buy_submit_plan", summary.buy_submit_plan),
        ):
            if candidate is not None and not isinstance(candidate, ExecutionSubmitPlan):
                raise ValueError(f"research_dict_only_submit_plan_not_authority:{field_name}")
        return (
            summary.typed_target_submit_plan()
            or summary.typed_residual_submit_plan()
            or summary.typed_buy_submit_plan()
        )

    def simulate_submit_plan(
        self,
        *,
        submit_plan: ExecutionSubmitPlan,
        context: ResearchExecutionContext,
        reference_price: float,
    ) -> ExecutionFill | None:
        if not isinstance(submit_plan, ExecutionSubmitPlan):
            raise ValueError("research_submit_plan_not_typed")
        request = execution_submit_plan_to_research_request(
            submit_plan=submit_plan,
            context=context,
            reference_price=reference_price,
            fee_rate=float(self.fee_rate),
        )
        if request is None:
            return None
        return self.execution_model.simulate(request)


def _positive_float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0.0 else None
