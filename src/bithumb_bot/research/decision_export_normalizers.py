from __future__ import annotations

from typing import Any

from bithumb_bot.canonical_decision import canonical_payload_hash, export_research_decisions
from bithumb_bot.strategy.market_regime import classify_sma_market_regime
from .hashing import sha256_prefixed


def decision_export_execution_timing_policy_hash() -> str:
    from .hashing import sha256_prefixed

    return sha256_prefixed({"runtime_replay": "closed_candle_through_ts"})


def generic_promotion_grade_research_export_decisions(
    *,
    raw_decisions: list[dict[str, object]],
    snapshot: object,
    params: dict[str, object],
    profile: dict[str, object],
    order_rules_hash: str,
) -> list[dict[str, object]]:
    profile_hash = str(
        profile.get("profile_content_hash")
        or (raw_decisions[0].get("profile_content_hash") if raw_decisions else "")
        or ""
    )
    decisions = export_research_decisions(
        raw_decisions,
        profile_content_hash=profile_hash,
        dataset_content_hash=snapshot.content_hash(),  # type: ignore[attr-defined]
        execution_timing_policy_hash=decision_export_execution_timing_policy_hash(),
    )
    cost = profile.get("cost_model") if isinstance(profile.get("cost_model"), dict) else {}
    fee_rate = str(float(cost.get("fee_rate", 0.0) or 0.0))
    stable_fee_model = {
        "bid_fee": fee_rate,
        "ask_fee": fee_rate,
        "fee_source": "chance_doc",
        "degraded": False,
        "degraded_reason": "none",
    }
    effective_params = (
        dict(profile.get("strategy_parameters"))
        if isinstance(profile.get("strategy_parameters"), dict)
        else dict(params)
    )
    slippage_model = {
        "exit_slippage_bps": float(cost.get("slippage_bps", 0.0) or 0.0),
        "exit_buffer_ratio": float(effective_params.get("ENTRY_EDGE_BUFFER_RATIO", 0.0) or 0.0),
    }
    aligned_decisions: list[dict[str, object]] = []
    for decision in decisions:
        decision["candidate_profile_hash"] = str(profile.get("candidate_profile_hash") or "")
        decision["approved_profile_hash"] = profile_hash
        decision["runtime_decision_request_hash"] = sha256_prefixed(
            {
                "source": "research_export_decision",
                "market": decision.get("market"),
                "interval": decision.get("interval"),
                "candle_ts": decision.get("candle_ts"),
                "strategy_name": decision.get("strategy_name"),
                "profile_content_hash": profile_hash,
            }
        )
        decision["runtime_strategy_set_manifest_hash"] = sha256_prefixed(
            {
                "runtime_strategy_set_manifest": {
                    "strategy_name": str(decision.get("strategy_name") or ""),
                    "strategy_instance_id": str(decision.get("strategy_instance_id") or ""),
                    "market": str(decision.get("market") or ""),
                    "interval": str(decision.get("interval") or ""),
                    "source": "runtime_replay_single_strategy",
                }
            }
        )
        decision["db_data_fingerprint"] = snapshot.content_hash()  # type: ignore[attr-defined]
        decision["candle_basis"] = "closed_candle"
        decision["decision_ts"] = None
        decision["fee_authority_hash"] = canonical_payload_hash(stable_fee_model)
        decision["fee_model_hash"] = canonical_payload_hash(stable_fee_model)
        decision["slippage_model_hash"] = canonical_payload_hash(slippage_model)
        decision["order_rules_hash"] = order_rules_hash
        authority = dict(decision.get("position_authority") if isinstance(decision.get("position_authority"), dict) else {})
        if (
            str(decision.get("final_signal") or "").upper() == "HOLD"
            and str(authority.get("state_class") or "") == "open_exposure"
            and not str(authority.get("unsupported_reason") or "").strip()
        ):
            decision["exit_reason"] = "no exit rule triggered"
        decision["exit_evaluations_hash"] = canonical_payload_hash(())
        authority["position_state_hash"] = str(decision.get("position_state_hash") or "")
        authority["order_rules_hash"] = str(decision.get("order_rules_hash") or "")
        authority["fee_authority_hash"] = str(decision.get("fee_authority_hash") or "")
        decision["position_authority"] = authority
        _refresh_strategy_behavior_hash(decision)
        aligned_decisions.append(decision)
    return aligned_decisions


def sma_promotion_grade_research_export_decisions(
    *,
    raw_decisions: list[dict[str, object]],
    snapshot: object,
    params: dict[str, object],
    profile: dict[str, object],
    order_rules_hash: str,
) -> list[dict[str, object]]:
    decisions = generic_promotion_grade_research_export_decisions(
        raw_decisions=raw_decisions,
        snapshot=snapshot,
        params=params,
        profile=profile,
        order_rules_hash=order_rules_hash,
    )
    effective_params = (
        dict(profile.get("strategy_parameters"))
        if isinstance(profile.get("strategy_parameters"), dict)
        else dict(params)
    )
    candles = list(getattr(snapshot, "candles", ()) or ())
    min_rows = max(
        int(effective_params.get("SMA_LONG", 0) or 0) + 2,
        int(effective_params.get("SMA_FILTER_VOL_WINDOW", 1) or 1),
        int(effective_params.get("SMA_FILTER_OVEREXT_LOOKBACK", 1) or 1) + 1,
    )
    aligned_decisions: list[dict[str, object]] = []
    for decision in decisions:
        candle_ts = int(decision.get("candle_ts") or 0)
        through = [candle for candle in candles if int(candle.ts) <= candle_ts]
        if len(through) < min_rows:
            continue
        if through:
            regime = classify_sma_market_regime(
                closes=[float(candle.close) for candle in through],
                short_sma=float(_strategy_payload_value(decision, "curr_s") or 0.0),
                long_sma=float(_strategy_payload_value(decision, "curr_l") or 0.0),
                volatility_window=max(1, int(effective_params.get("SMA_FILTER_VOL_WINDOW", 10) or 10)),
                min_volatility_ratio=float(effective_params.get("SMA_FILTER_VOL_MIN_RANGE_RATIO", 0.0) or 0.0),
                overextended_lookback=max(1, int(effective_params.get("SMA_FILTER_OVEREXT_LOOKBACK", 3) or 3)),
                overextended_max_return_ratio=float(
                    effective_params.get("SMA_FILTER_OVEREXT_MAX_RETURN_RATIO", 0.0) or 0.0
                ),
                min_trend_strength_ratio=float(effective_params.get("SMA_FILTER_GAP_MIN_RATIO", 0.0) or 0.0),
            )
            decision["market_regime"] = regime.composite_regime
            decision["regime_decision"] = "ON"
            decision["regime_block_reason"] = "none"
        aligned_decisions.append(decision)
    return aligned_decisions


def _strategy_payload_value(decision: dict[str, Any], key: str) -> object:
    payload = decision.get("strategy_specific_payload")
    if isinstance(payload, dict) and key in payload:
        return payload[key]
    return decision.get(key)


def _refresh_strategy_behavior_hash(decision: dict[str, object]) -> None:
    payload = {
        "strategy_name": str(decision.get("strategy_name") or ""),
        "strategy_version": str(decision.get("strategy_version") or ""),
        "strategy_decision_contract_version": str(decision.get("strategy_decision_contract_version") or ""),
        "raw_signal": str(decision.get("raw_signal") or "").upper(),
        "final_signal": str(decision.get("final_signal") or decision.get("side") or "").upper(),
        "strategy_specific_payload": (
            dict(decision.get("strategy_specific_payload"))
            if isinstance(decision.get("strategy_specific_payload"), dict)
            else {}
        ),
    }
    decision["strategy_behavior_payload"] = payload
    decision["strategy_behavior_hash"] = canonical_payload_hash(payload)
