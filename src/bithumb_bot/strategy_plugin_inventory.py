from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from bithumb_bot.research.strategy_registry import (
    ResearchStrategyRegistryError,
    list_research_strategy_plugins,
    resolve_research_strategy_plugin,
)


SUPPORTED_STRATEGY_VALIDATION_TARGETS = (
    "research_backtest",
    "runtime_replay",
    "runtime_decision",
    "live_dry_run",
    "live_real_order",
)


@dataclass(frozen=True)
class StrategyPluginSource:
    source: str
    manifest_object_path: str | None = None
    entry_point_name: str | None = None
    entry_point_value: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "source": self.source,
            "manifest_object_path": self.manifest_object_path,
            "entry_point_name": self.entry_point_name,
            "entry_point_value": self.entry_point_value,
        }


def build_strategy_plugin_inventory() -> dict[str, Any]:
    """Build a deterministic, read-only inventory of discovered strategy plugins."""

    plugins = list_research_strategy_plugins()
    source_by_name = _strategy_plugin_sources_by_name()
    entries: list[dict[str, Any]] = []
    for plugin in plugins:
        payload = plugin.contract_payload()
        source = source_by_name.get(plugin.name, StrategyPluginSource(source="unknown")).as_dict()
        live_eligibility = dict(payload["live_eligibility"])
        entries.append(
            {
                "name": plugin.name,
                "strategy_name": payload["strategy_name"],
                "version": plugin.version,
                "source": source["source"],
                "manifest_object_path": source["manifest_object_path"],
                "entry_point_name": source["entry_point_name"],
                "entry_point_value": source["entry_point_value"],
                "authoring_contract_kind": payload["authoring_contract_kind"],
                "authoring_level": payload["authoring_level"],
                "canonical_authoring_level": payload["canonical_authoring_level"],
                "legacy_authoring_level_alias": payload["legacy_authoring_level_alias"],
                "capability_level": payload["capability_level"],
                "operational_capability": payload["operational_capability"],
                "operator_verdict": payload["operator_verdict"],
                "supported_runtime_scope": payload["supported_runtime_scope"],
                "parameter_authority": payload["parameter_authority"],
                "legacy_fallback": payload["legacy_fallback"],
                "required_evidence_summary": payload["required_evidence_summary"],
                "contract_hash": plugin.contract_hash(),
                "strategy_spec_hash": payload["strategy_spec_hash"],
                "runtime_capabilities": payload["runtime_capabilities"],
                "runtime_replay_supported": payload["runtime_replay_supported"],
                "runtime_decision_supported": payload["runtime_decision_supported"],
                "live_dry_run_allowed": payload["live_dry_run_allowed"],
                "live_real_order_allowed": payload["live_real_order_allowed"],
                "approved_profile_required": payload["approved_profile_required"],
                "runtime_data_requirements": payload["runtime_data_requirements"],
                "risk_profile_required": payload["risk_profile_required"],
                "promotion_evidence_required": payload["promotion_evidence_required"],
                "next_required_action": payload["next_required_action"],
                "live_eligibility": live_eligibility,
                "fail_closed_reason": live_eligibility["fail_closed_reason"],
                "decision_evidence_contract": {
                    "contract_hash": payload["decision_evidence_contract"]["contract_hash"],
                },
                "required_data": list(plugin.required_data),
                "optional_data": list(plugin.optional_data),
            }
        )
    entries.sort(key=lambda item: str(item["name"]))
    return {
        "schema_version": 1,
        "strategy_count": len(entries),
        "strategies": entries,
    }


def strategy_plugin_inventory_json() -> str:
    return json.dumps(
        build_strategy_plugin_inventory(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_strategy_target_verdict(
    strategy: str,
    target: str,
) -> dict[str, Any]:
    normalized_target = str(target or "").strip().lower()
    if normalized_target not in SUPPORTED_STRATEGY_VALIDATION_TARGETS:
        raise ValueError(f"strategy_validation_target_unsupported:{normalized_target}")
    strategy_name = str(strategy or "").strip().lower()
    if not strategy_name:
        raise ValueError("strategy_validation_strategy_missing")
    try:
        plugin = resolve_research_strategy_plugin(strategy_name)
    except ResearchStrategyRegistryError as exc:
        return _unknown_strategy_verdict(strategy_name, normalized_target, str(exc))
    payload = plugin.contract_payload()
    operator_targets = dict(payload["operator_verdict"]["targets"])
    static_verdict = dict(operator_targets[normalized_target])
    required_evidence = _required_evidence_for_target(payload, normalized_target)
    blocking_reasons = list(static_verdict.get("blocked_reasons") or [])
    if normalized_target in {"live_dry_run", "live_real_order"} and bool(
        payload["live_eligibility"]["approved_profile_required"]
    ):
        reason = f"approved_profile_required_for_strategy:{plugin.name}"
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)
    if normalized_target == "live_real_order" and not bool(payload["live_real_order_allowed"]):
        reason = f"live_real_order_not_allowed_for_strategy:{plugin.name}:{payload['fail_closed_reason']}"
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)
    if normalized_target == "live_dry_run" and not bool(payload["live_dry_run_allowed"]):
        reason = f"live_dry_run_not_allowed_for_strategy:{plugin.name}:{payload['fail_closed_reason']}"
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)
    allowed = bool(static_verdict.get("allowed")) and not blocking_reasons
    return {
        "strategy": plugin.name,
        "authoring_level": payload["authoring_level"],
        "capability_level": payload["capability_level"],
        "target_runtime": normalized_target,
        "allowed": bool(allowed),
        "blocking_reasons": [] if allowed else sorted(set(str(item) for item in blocking_reasons)),
        "next_required_action": "none"
        if allowed
        else _next_action_for_blocked_target(payload, normalized_target, blocking_reasons),
        "required_evidence": required_evidence,
        "supported_runtime_scope": payload["supported_runtime_scope"],
    }


def strategy_target_verdict_json(strategy: str, target: str) -> str:
    return json.dumps(
        build_strategy_target_verdict(strategy, target),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _required_evidence_for_target(payload: dict[str, Any], target: str) -> dict[str, bool]:
    runtime_target = target in {"runtime_decision", "live_dry_run", "live_real_order"}
    live_target = target in {"live_dry_run", "live_real_order"}
    return {
        "approved_profile": bool(live_target and payload["live_eligibility"]["approved_profile_required"]),
        "runtime_contract_hash": bool(runtime_target),
        "decision_evidence_contract": bool(runtime_target or target == "runtime_replay"),
        "runtime_data_preflight": bool(runtime_target),
        "risk_profile": bool(live_target),
    }


def _next_action_for_blocked_target(
    payload: dict[str, Any],
    target: str,
    blocking_reasons: list[str],
) -> str:
    if any("approved_profile_required_for_strategy" in reason for reason in blocking_reasons):
        return "supply_approved_profile"
    if target == "runtime_replay":
        return "add_replay_compatible_contract"
    if target == "runtime_decision":
        return "add_live_eligible_contract_for_runtime_or_live"
    if target == "live_dry_run":
        return "add_live_dry_run_capability"
    if target == "live_real_order":
        return "add_live_real_order_eligible_contract"
    return str(payload.get("next_required_action") or "do_not_promote")


def _unknown_strategy_verdict(strategy_name: str, target: str, reason: str) -> dict[str, Any]:
    return {
        "strategy": strategy_name,
        "authoring_level": "unknown",
        "capability_level": "unsupported",
        "target_runtime": target,
        "allowed": False,
        "blocking_reasons": [f"strategy_plugin_not_registered:{strategy_name}:{reason}"],
        "next_required_action": "register_strategy_plugin",
        "required_evidence": _required_evidence_for_target(
            {"live_eligibility": {"approved_profile_required": True}},
            target,
        ),
        "supported_runtime_scope": {
            "supported_runtime_scope": "multi_strategy_single_pair_single_interval",
            "multi_pair_portfolio_supported": False,
            "multi_interval_runtime_supported": False,
        },
    }


def _strategy_plugin_sources_by_name() -> dict[str, StrategyPluginSource]:
    from bithumb_bot.strategy_plugins import coerce_loaded_strategy_plugins, metadata
    from bithumb_bot.strategy_plugins.builtin_manifest import iter_builtin_strategy_plugin_exports

    sources: dict[str, StrategyPluginSource] = {}
    for plugin_export in iter_builtin_strategy_plugin_exports():
        loaded = plugin_export.load()
        for plugin in coerce_loaded_strategy_plugins(loaded):
            sources.setdefault(
                plugin.name,
                StrategyPluginSource(
                    source="built_in_manifest",
                    manifest_object_path=plugin_export.object_path,
                ),
            )

    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        selected = entry_points.select(group="bithumb_bot.strategy_plugins")
    elif isinstance(entry_points, dict):
        selected = entry_points.get("bithumb_bot.strategy_plugins", ())
    else:
        selected = [
            item
            for item in entry_points
            if str(getattr(item, "group", "bithumb_bot.strategy_plugins"))
            == "bithumb_bot.strategy_plugins"
        ]
    for entry_point in sorted(
        selected,
        key=lambda item: (
            str(getattr(item, "name", "")),
            str(getattr(item, "value", "")),
        ),
    ):
        for plugin in coerce_loaded_strategy_plugins(entry_point.load()):
            sources.setdefault(
                plugin.name,
                StrategyPluginSource(
                    source="entry_point",
                    entry_point_name=str(getattr(entry_point, "name", "")),
                    entry_point_value=str(getattr(entry_point, "value", "")),
                ),
            )
    return sources
