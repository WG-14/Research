from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .hashing import canonical_payload_hash

from . import backtest_support as support


@dataclass(frozen=True)
class DecisionPayloadBuilder:
    """Builds non-authoritative research decision observability payloads."""

    def build(
        self,
        *,
        detail_level: str,
        **kwargs: Any,
    ) -> dict[str, object]:
        detail = str(detail_level or "").strip().lower()
        if detail == "summary":
            return self.build_summary(**kwargs)
        if detail == "full_canonical":
            return self.build_full(**kwargs)
        raise ValueError("decision_payload_detail_level_required")

    def build_summary(
        self,
        *,
        dataset: Any,
        dataset_content_hash: str,
        parameter_values: dict[str, Any],
        strategy_plugin: Any,
        strategy_spec: Any,
        exit_policy: dict[str, Any],
        exit_policy_hash: str,
        exit_policy_config_hash: str | None,
        fee_rate: float,
        slippage_bps: float,
        timing_policy: Any,
        portfolio_policy: Any,
        event: Any,
        decision_boundary_ts: int,
        strategy_envelope: Any,
        risk_decision: Any,
        policy_position: Any,
        policy_decision: Any | None,
        regime_snapshot: dict[str, object],
        qty: float,
        sellable_qty: float,
        canonical_context: Any | None = None,
    ) -> dict[str, object]:
        surface = _decision_surface(
            event=event,
            strategy_envelope=strategy_envelope,
            risk_decision=risk_decision,
            policy_decision=policy_decision,
            sellable_qty=sellable_qty,
        )
        strategy_spec_hash = _context_hash(canonical_context, "strategy_spec_hash")
        if not strategy_spec_hash:
            strategy_spec_hash = strategy_spec.spec_hash()
        strategy_plugin_contract_hash = _context_hash(
            canonical_context,
            "strategy_plugin_contract_hash",
        )
        if not strategy_plugin_contract_hash:
            strategy_plugin_contract_hash = strategy_plugin.contract_hash()
        execution_timing_policy_hash = _context_hash(
            canonical_context,
            "execution_timing_policy_hash",
        )
        if not execution_timing_policy_hash:
            execution_timing_policy_hash = canonical_payload_hash(
                timing_policy.as_dict(),
                label="execution_timing_policy_summary_fallback",
            )
        active_exit_policy_config_hash = _context_hash(
            canonical_context,
            "active_exit_policy_config_hash",
        )
        if not active_exit_policy_config_hash:
            active_exit_policy_config_hash = str(exit_policy_config_hash or "")
        feature_snapshot_hash = str(event.feature_snapshot.get("feature_snapshot_hash") or "").strip()
        if not feature_snapshot_hash:
            feature_snapshot_hash = canonical_payload_hash(
                {
                    "candle_ts": int(event.candle_ts),
                    "feature_keys": sorted(str(key) for key in event.feature_snapshot),
                },
                label="summary_feature_snapshot_reference",
            )
        strategy_behavior_hash = str(
            getattr(strategy_envelope, "replay_fingerprint_hash", "")
            or surface["raw_signal"]
            or ""
        )
        if not strategy_behavior_hash.startswith("sha256:"):
            strategy_behavior_hash = canonical_payload_hash(
                {
                    "strategy_name": str(strategy_plugin.name),
                    "raw_signal": surface["raw_signal"],
                    "final_signal": surface["final_signal"],
                    "reason": str(risk_decision.reason_code),
                },
                label="summary_strategy_behavior_reference",
            )
        position_payload = (
            policy_position.as_dict()
            if hasattr(policy_position, "as_dict")
            else vars(policy_position)
            if hasattr(policy_position, "__dict__")
            else {}
        )
        position_state_hash = canonical_payload_hash(
            position_payload,
            label="summary_position_state",
        )
        payload: dict[str, object] = {
            "decision_event_schema_version": 1,
            "decision_payload_detail_level": "summary",
            "canonical_evidence_policy": "summary_aggregate",
            "strategy_name": str(strategy_plugin.name),
            "strategy_decision_contract_version": strategy_plugin.decision_contract_version,
            "strategy_spec_hash": strategy_spec_hash,
            "strategy_plugin_contract_hash": strategy_plugin_contract_hash,
            "dataset_content_hash": str(dataset_content_hash),
            "candidate_profile_hash": _context_hash(canonical_context, "candidate_profile_hash")
            or canonical_payload_hash(
                {
                    "strategy_name": str(strategy_plugin.name),
                    "parameter_values": parameter_values,
                    "strategy_spec_hash": strategy_spec_hash,
                    "strategy_plugin_contract_hash": strategy_plugin_contract_hash,
                    "exit_policy_hash": exit_policy_hash,
                },
                label="summary_candidate_profile",
            ),
            "parameter_values_hash": _context_hash(canonical_context, "parameter_values_hash")
            or canonical_payload_hash(
                parameter_values,
                label="summary_parameter_values",
            ),
            "exit_policy_hash": str(exit_policy_hash),
            "exit_policy_config_hash": active_exit_policy_config_hash,
            "market": dataset.market,
            "interval": dataset.interval,
            "signal_timestamp": str(event.candle_ts),
            "candle_ts": int(event.candle_ts),
            "through_ts_ms": int(event.candle_ts),
            "candle_basis": "research_closed_candle",
            "decision_ts": int(decision_boundary_ts),
            "raw_signal": surface["raw_signal"],
            "entry_signal": surface["entry_signal"],
            "exit_signal": surface["exit_signal"],
            "final_signal": surface["final_signal"],
            "side": surface["final_signal"],
            "entry_reason": str(risk_decision.reason_code),
            "blocked": bool(risk_decision.block or (surface["raw_signal"] in {"BUY", "SELL"} and surface["final_signal"] == "HOLD")),
            "block_reason": str(risk_decision.reason_code) if bool(risk_decision.block) else "",
            "blocked_filters": tuple(surface["blocked_filters"]),
            "feature_snapshot_hash": feature_snapshot_hash,
            "strategy_behavior_hash": strategy_behavior_hash,
            "strategy_specific_payload_hash": strategy_behavior_hash,
            "position_state_hash": position_state_hash,
            "execution_timing_policy_hash": execution_timing_policy_hash,
            "exit_rule": str(risk_decision.exit_rule or ""),
            "exit_reason": str(risk_decision.exit_reason or ""),
            "current_market_regime_snapshot_hash": canonical_payload_hash(
                regime_snapshot,
                label="summary_regime_snapshot",
            ),
            "regime_decision": "summary_not_materialized",
            "regime_block_reason": "",
            "qty": float(qty),
            "sellable_qty": float(sellable_qty),
            "replay_fingerprint_hash": str(getattr(strategy_envelope, "replay_fingerprint_hash", "") or ""),
            "strategy_diagnostics_namespace": strategy_plugin.diagnostics_namespace,
            "execution_intent": str(surface["final_signal"]).lower() if surface["final_signal"] in {"BUY", "SELL"} else "none",
            "order_intent": dict(event.order_intent) if event.order_intent is not None else None,
            "exit_intent": dict(event.exit_intent) if event.exit_intent is not None else None,
            "research_policy_recomputed_with_simulated_position": policy_decision is not None,
            "research_policy_unsupported": bool(strategy_envelope.unsupported_reason),
            "research_policy_unsupported_reason": strategy_envelope.unsupported_reason,
            "research_policy_comparable": not bool(strategy_envelope.unsupported_reason),
            "research_comparable": bool(strategy_envelope.provenance.get("research_comparable")),
        }
        if policy_decision is not None:
            payload["pure_policy_hash"] = policy_decision.policy_hash
            payload["policy_contract_hash"] = policy_decision.policy_contract_hash
            payload["policy_input_hash"] = policy_decision.policy_input_hash
            payload["policy_decision_hash"] = policy_decision.policy_decision_hash
            trace = policy_decision.as_trace()
            for key in (
                "entry_signal_source",
                "entry_sizing_source",
                "count_basis",
                "kst_day",
                "daily_count_snapshot_hash",
                "daily_count_snapshot_event_set_hash",
                "participation_policy_hash",
                "participation_input_hash",
                "participation_decision_hash",
                "fallback_mode",
                "not_a_fill_guarantee",
            ):
                if key in trace:
                    payload[key] = trace[key]
        statistical_evidence = bool(getattr(strategy_plugin, "is_statistical_evidence", False))
        payload["statistical_evidence"] = statistical_evidence
        payload["validation_extension_missing_reason"] = (
            ""
            if statistical_evidence
            else str(getattr(getattr(strategy_plugin, "research_contract", None), "fail_closed_reason", ""))
        )
        payload["recommended_next_action"] = "none" if statistical_evidence else "review_strategy_contract"
        _attach_common_exit_diagnostic_counts(payload)
        return payload

    def build_full(
        self,
        *,
        dataset: Any,
        dataset_content_hash: str,
        parameter_values: dict[str, Any],
        strategy_plugin: Any,
        strategy_spec: Any,
        exit_policy: dict[str, Any],
        exit_policy_hash: str,
        exit_policy_config_hash: str | None,
        fee_rate: float,
        slippage_bps: float,
        timing_policy: Any,
        portfolio_policy: Any,
        event: Any,
        decision_boundary_ts: int,
        strategy_envelope: Any,
        risk_decision: Any,
        policy_position: Any,
        policy_decision: Any | None,
        regime_snapshot: dict[str, object],
        qty: float,
        sellable_qty: float,
        canonical_context: Any | None = None,
    ) -> dict[str, object]:
        action = risk_decision.final_signal
        raw_signal = str(strategy_envelope.provenance.get("raw_signal") or "HOLD").upper()
        raw_reason = str(strategy_envelope.provenance.get("raw_reason") or event.reason)
        raw_filter_would_block = bool(strategy_envelope.provenance.get("raw_filter_would_block"))
        entry_signal = str(strategy_envelope.provenance.get("entry_signal") or raw_signal).upper()
        exit_signal = str(strategy_envelope.provenance.get("exit_signal") or raw_signal).upper()
        blocked_filters = list(strategy_envelope.provenance.get("blocked_filters") or ())
        entry_decision = strategy_envelope.provenance.get("entry_decision")
        market_regime_decision = (
            dict(getattr(entry_decision, "candidate_regime_decision"))
            if entry_decision is not None
            and isinstance(getattr(entry_decision, "candidate_regime_decision", None), dict)
            else {"regime_decision": "not_configured"}
        )
        market_regime_blocked = bool(
            getattr(entry_decision, "market_regime_triggered", False) if entry_decision is not None else False
        )
        candidate_regime_blocked = bool(
            getattr(entry_decision, "candidate_regime_triggered", False) if entry_decision is not None else False
        )
        if policy_decision is not None:
            protective_exit_overrode_entry = bool(policy_decision.protective_exit_overrode_entry)
            entry_blocked = bool(policy_decision.entry_blocked)
            exit_filter_suppression_prevented = bool(policy_decision.exit_filter_suppression_prevented)
        elif strategy_envelope.unsupported_reason:
            protective_exit_overrode_entry = False
            entry_blocked = False
            exit_filter_suppression_prevented = False
        else:
            protective_exit_overrode_entry = bool(
                raw_signal == "BUY"
                and action == "SELL"
                and risk_decision.exit_rule in {"stop_loss", "max_holding_time"}
            )
            entry_blocked = bool(raw_signal == "BUY" and action == "HOLD" and raw_filter_would_block)
            exit_filter_suppression_prevented = bool(
                raw_signal == "SELL"
                and raw_filter_would_block
                and sellable_qty > 1e-12
                and bool(risk_decision.exit_evaluations)
            )
        strategy_spec_hash = _context_hash(canonical_context, "strategy_spec_hash")
        if not strategy_spec_hash:
            strategy_spec_hash = strategy_spec.spec_hash()
        strategy_plugin_contract_hash = _context_hash(
            canonical_context,
            "strategy_plugin_contract_hash",
        )
        if not strategy_plugin_contract_hash:
            strategy_plugin_contract_hash = strategy_plugin.contract_hash()
        execution_timing_policy_hash = _context_hash(
            canonical_context,
            "execution_timing_policy_hash",
        )
        fee_model_hash = _context_hash(canonical_context, "fee_model_hash")
        slippage_model_hash = _context_hash(canonical_context, "slippage_model_hash")
        candidate_profile_hash = _context_hash(canonical_context, "candidate_profile_hash")
        parameter_values_hash = _context_hash(canonical_context, "parameter_values_hash")
        fee_authority_hash = _context_hash(canonical_context, "fee_authority_hash")
        order_rules_hash = _context_hash(canonical_context, "order_rules_hash")
        active_exit_policy_config_hash = _context_hash(
            canonical_context,
            "active_exit_policy_config_hash",
        )
        if not active_exit_policy_config_hash:
            active_exit_policy_config_hash = str(exit_policy_config_hash or "")
        payload = support.research_decision_payload(
            dataset=dataset,
            dataset_content_hash=dataset_content_hash,
            parameter_values=parameter_values,
            strategy_name=strategy_plugin.name,
            strategy_spec=strategy_spec.as_dict(),
            strategy_spec_hash=strategy_spec_hash,
            strategy_plugin_contract=strategy_plugin.contract_payload(),
            strategy_plugin_contract_hash=strategy_plugin_contract_hash,
            exit_policy=exit_policy,
            exit_policy_hash=exit_policy_hash,
            exit_policy_config_hash=active_exit_policy_config_hash,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            timing_policy=timing_policy,
            portfolio_policy=portfolio_policy,
            execution_timing_policy_hash=execution_timing_policy_hash,
            fee_model_hash=fee_model_hash,
            slippage_model_hash=slippage_model_hash,
            candidate_profile_hash=candidate_profile_hash,
            parameter_values_hash=parameter_values_hash,
            fee_authority_hash=fee_authority_hash,
            order_rules_hash=order_rules_hash,
            candle_ts=event.candle_ts,
            decision_ts=decision_boundary_ts,
            raw_signal=raw_signal,
            entry_signal=entry_signal,
            exit_signal=exit_signal,
            final_signal=action,
            raw_reason=raw_reason,
            blocked=bool(risk_decision.block or (raw_signal in {"BUY", "SELL"} and action == "HOLD")),
            raw_filter_would_block=raw_filter_would_block,
            entry_blocked=entry_blocked,
            protective_exit_overrode_entry=protective_exit_overrode_entry,
            exit_filter_suppression_prevented=exit_filter_suppression_prevented,
            blocked_filters=blocked_filters,
            feature_snapshot=dict(event.feature_snapshot),
            regime_snapshot=regime_snapshot,
            entry_reason=risk_decision.reason_code,
            market_regime_decision=market_regime_decision,
            market_regime_blocked=market_regime_blocked,
            candidate_regime_blocked=candidate_regime_blocked,
            qty=qty,
            sellable_qty=sellable_qty,
            exit_rule=risk_decision.exit_rule,
            exit_reason=risk_decision.exit_reason,
            exit_evaluations=[dict(item) for item in risk_decision.exit_evaluations],
        )
        statistical_evidence = False
        payload_adapter = getattr(strategy_plugin, "payload_adapter", None)
        if payload_adapter is not None:
            payload = payload_adapter(payload, policy_decision if policy_decision is not None else event)
        validation_missing_reason = "research_strategy_catalog"
        payload.update(
            {
                "decision_event_schema_version": 1,
                "strategy_decision_contract_version": strategy_plugin.decision_contract_version,
                "statistical_evidence": statistical_evidence,
                "validation_extension_missing_reason": validation_missing_reason,
                "recommended_next_action": "none" if statistical_evidence else "review_strategy_contract",
                "raw_reason": raw_reason,
                "feature_snapshot": dict(event.feature_snapshot),
                "strategy_diagnostics_namespace": strategy_plugin.diagnostics_namespace,
                "strategy_diagnostics": dict(event.strategy_diagnostics),
                "strategy_behavior_payload": {
                    "strategy_name": event.strategy_name,
                    "strategy_version": event.strategy_version,
                    "raw_signal": raw_signal,
                    "final_signal": action,
                    "reason": risk_decision.reason_code,
                    "feature_snapshot": dict(event.feature_snapshot),
                    "strategy_diagnostics": dict(event.strategy_diagnostics),
                },
                "execution_intent": action.lower() if action in {"BUY", "SELL"} else "none",
                "order_intent": dict(event.order_intent) if event.order_intent is not None else None,
                "exit_intent": dict(event.exit_intent) if event.exit_intent is not None else None,
                "research_policy_position_terminal_state": policy_position.terminal_state,
                "research_policy_recomputed_with_simulated_position": policy_decision is not None,
                "research_policy_unsupported": bool(strategy_envelope.unsupported_reason),
                "research_policy_unsupported_reason": strategy_envelope.unsupported_reason,
                "research_policy_comparable": not bool(strategy_envelope.unsupported_reason),
                "research_comparable": bool(strategy_envelope.provenance.get("research_comparable")),
            }
        )
        risk_payload = risk_decision.payload if isinstance(risk_decision.payload, dict) else {}
        for key in (
            "risk_input_hash",
            "risk_policy_hash",
            "risk_evidence_hash",
            "risk_decision_hash",
            "risk_reason_code",
            "risk_status",
            "risk_evaluation_point",
            "risk_state_source",
            "effective_risk_limits",
        ):
            if key in risk_payload:
                payload[key] = risk_payload[key]
        if "risk_decision" in risk_payload:
            payload["risk_decision"] = risk_payload["risk_decision"]
        if strategy_plugin.diagnostics_builder is not None and "strategy_diagnostic_counts" not in payload:
            diagnostic_contract = strategy_plugin.diagnostics_builder(payload)
            if not isinstance(diagnostic_contract, dict):
                raise TypeError("strategy_diagnostics_count_builder_must_return_dict")
            defaults = diagnostic_contract.get("strategy_diagnostic_count_defaults")
            counts = diagnostic_contract.get("strategy_diagnostic_counts")
            if isinstance(defaults, dict):
                payload["strategy_diagnostic_count_defaults"] = defaults
            if isinstance(counts, dict):
                payload["strategy_diagnostic_counts"] = counts
            payload["strategy_diagnostic_counts_authority"] = "diagnostic_non_authoritative"
        _attach_common_exit_diagnostic_counts(payload)
        if policy_decision is not None:
            payload["pure_policy_hash"] = policy_decision.policy_hash
            payload["policy_contract_hash"] = policy_decision.policy_contract_hash
            payload["policy_input_hash"] = policy_decision.policy_input_hash
            payload["policy_decision_hash"] = policy_decision.policy_decision_hash
            payload["pure_policy_trace"] = policy_decision.as_trace()
            trace = policy_decision.as_trace()
            for key in (
                "decision_input_bundle_hash",
                "decision_input_contract_hash",
                "decision_input_bundle_payload_hash",
                "snapshot_projector_version",
                "snapshot_projector_hash",
                "materialized_parameters_hash",
                "market_snapshot_hash",
                "market_feature_hash",
                "canonical_feature_projection_hash",
                "final_exit_decision_input_hash",
                "position_snapshot_hash",
                "execution_constraints_hash",
                "policy_config_hash",
                "replay_fingerprint_hash",
                "entry_signal_source",
                "entry_sizing_source",
                "count_basis",
                "kst_day",
                "daily_count_snapshot_hash",
                "daily_count_snapshot_event_set_hash",
                "participation_policy_hash",
                "participation_input_hash",
                "participation_decision_hash",
                "fallback_mode",
                "not_a_fill_guarantee",
            ):
                if str(trace.get(key) or "").strip():
                    payload[key] = trace[key]
            service_provenance = trace.get("strategy_evaluation_provenance")
            if isinstance(service_provenance, dict):
                payload["strategy_evaluation_provenance"] = dict(service_provenance)
            payload["execution_intent_v2"] = (
                policy_decision.execution_intent.as_dict()
                if policy_decision.execution_intent is not None
                else None
            )
            diagnostics = (
                dict(payload["strategy_diagnostics"])
                if isinstance(payload.get("strategy_diagnostics"), dict)
                else {}
            )
            diagnostics.update(
                {
                    "pure_policy_hash": policy_decision.policy_hash,
                    "policy_contract_hash": policy_decision.policy_contract_hash,
                    "policy_input_hash": policy_decision.policy_input_hash,
                    "policy_decision_hash": policy_decision.policy_decision_hash,
                    "pure_policy_trace": policy_decision.as_trace(),
                    "policy_position_terminal_state": policy_position.terminal_state,
                    "policy_recomputed_with_simulated_position": True,
                }
            )
            payload["strategy_diagnostics"] = diagnostics
        if str(exit_policy_config_hash or "").strip() and not str(
            payload.get("exit_policy_config_hash") or ""
        ).strip():
            payload["exit_policy_config_hash"] = str(exit_policy_config_hash)
        payload["decision_payload_detail_level"] = "full_canonical"
        return payload


def _decision_surface(
    *,
    event: Any,
    strategy_envelope: Any,
    risk_decision: Any,
    policy_decision: Any | None,
    sellable_qty: float,
) -> dict[str, object]:
    action = risk_decision.final_signal
    raw_signal = str(strategy_envelope.provenance.get("raw_signal") or "HOLD").upper()
    raw_reason = str(strategy_envelope.provenance.get("raw_reason") or event.reason)
    raw_filter_would_block = bool(strategy_envelope.provenance.get("raw_filter_would_block"))
    entry_signal = str(strategy_envelope.provenance.get("entry_signal") or raw_signal).upper()
    exit_signal = str(strategy_envelope.provenance.get("exit_signal") or raw_signal).upper()
    blocked_filters = list(strategy_envelope.provenance.get("blocked_filters") or ())
    if policy_decision is not None:
        entry_blocked = bool(policy_decision.entry_blocked)
        protective_exit_overrode_entry = bool(policy_decision.protective_exit_overrode_entry)
        exit_filter_suppression_prevented = bool(policy_decision.exit_filter_suppression_prevented)
    else:
        entry_blocked = bool(raw_signal == "BUY" and action == "HOLD" and raw_filter_would_block)
        protective_exit_overrode_entry = bool(
            raw_signal == "BUY"
            and action == "SELL"
            and risk_decision.exit_rule in {"stop_loss", "max_holding_time"}
        )
        exit_filter_suppression_prevented = bool(
            raw_signal == "SELL"
            and raw_filter_would_block
            and sellable_qty > 1e-12
            and bool(risk_decision.exit_evaluations)
        )
    return {
        "raw_signal": raw_signal,
        "raw_reason": raw_reason,
        "raw_filter_would_block": raw_filter_would_block,
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "final_signal": action,
        "blocked_filters": blocked_filters,
        "entry_blocked": entry_blocked,
        "protective_exit_overrode_entry": protective_exit_overrode_entry,
        "exit_filter_suppression_prevented": exit_filter_suppression_prevented,
    }


def _context_hash(canonical_context: Any | None, field: str) -> str:
    if canonical_context is not None:
        value = getattr(canonical_context, field, "")
        if str(value or "").strip():
            return str(value)
    return ""


def _attach_common_exit_diagnostic_counts(payload: dict[str, object]) -> None:
    increments: dict[str, int] = {}
    for evaluation in payload.get("exit_evaluations") or []:
        if not isinstance(evaluation, dict) or not bool(evaluation.get("triggered")):
            continue
        rule = str(evaluation.get("rule") or "")
        if rule == "stop_loss":
            increments["stop_loss_exit_count"] = increments.get("stop_loss_exit_count", 0) + 1
        elif rule == "max_holding_time":
            increments["max_holding_exit_count"] = increments.get("max_holding_exit_count", 0) + 1
    if not increments:
        return
    defaults = payload.get("strategy_diagnostic_count_defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        payload["strategy_diagnostic_count_defaults"] = defaults
    counts = payload.get("strategy_diagnostic_counts")
    if not isinstance(counts, dict):
        counts = {}
        payload["strategy_diagnostic_counts"] = counts
    for key, increment in increments.items():
        defaults.setdefault(key, 0)
        counts.setdefault(key, int(increment))


__all__ = ["DecisionPayloadBuilder"]
