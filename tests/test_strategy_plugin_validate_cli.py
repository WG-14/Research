from __future__ import annotations

import json

from bithumb_bot.cli.registry import command_registry
from bithumb_bot.strategy_plugin_inventory import (
    build_strategy_target_verdict,
    strategy_target_verdict_json,
)


def test_strategy_plugin_validate_command_is_read_only() -> None:
    spec = command_registry()["strategy-plugin-validate"]

    assert spec.read_only is True
    assert spec.mutating is False
    assert spec.writes_db is False
    assert spec.uses_broker is False
    assert spec.produces_artifact is False
    assert spec.json_output_supported is True


def test_level_1_research_strategy_target_verdicts_fail_closed_for_runtime() -> None:
    research = build_strategy_target_verdict("threshold_research_only", "research_backtest")
    runtime = build_strategy_target_verdict("threshold_research_only", "runtime_decision")

    assert research["allowed"] is True
    assert research["authoring_level"] == "level_1_research_only"
    assert runtime["allowed"] is False
    assert any("promotion_runtime_unsupported_for_strategy" in item for item in runtime["blocking_reasons"])
    assert runtime["next_required_action"] == "add_live_eligible_contract_for_runtime_or_live"


def test_level_2_replay_strategy_target_verdicts_fail_closed_for_live() -> None:
    replay = build_strategy_target_verdict("replay_threshold", "runtime_replay")
    live = build_strategy_target_verdict("replay_threshold", "live_real_order")

    assert replay["allowed"] is True
    assert replay["authoring_level"] == "level_2_replay_compatible"
    assert live["allowed"] is False
    assert any("live_real_order_not_allowed_for_strategy" in item for item in live["blocking_reasons"])


def test_level_3_promotion_grade_verdict_separates_runtime_from_live_authority() -> None:
    runtime = build_strategy_target_verdict("canary_non_sma", "runtime_decision")
    live_real = build_strategy_target_verdict("canary_non_sma", "live_real_order")
    live_disabled = build_strategy_target_verdict("safe_hold", "live_dry_run")

    assert runtime["allowed"] is True
    assert runtime["authoring_level"] == "level_3_promotion_grade"
    assert runtime["required_evidence"]["runtime_data_preflight"] is True
    assert live_real["allowed"] is False
    assert any("live_real_order_not_allowed_for_strategy" in item for item in live_real["blocking_reasons"])
    assert live_real["required_evidence"]["approved_profile"] is True
    assert live_disabled["authoring_level"] == "level_3_promotion_grade"
    assert live_disabled["capability_level"] == "runtime_decision"
    assert live_disabled["allowed"] is False
    assert any("live_dry_run_not_allowed_for_strategy" in item for item in live_disabled["blocking_reasons"])


def test_strategy_target_verdict_json_is_deterministic_and_scope_explicit() -> None:
    first = strategy_target_verdict_json("canary_non_sma", "runtime_decision")
    second = strategy_target_verdict_json("canary_non_sma", "runtime_decision")
    payload = json.loads(first)

    assert first == second
    assert payload["supported_runtime_scope"]["supported_runtime_scope"] == (
        "multi_strategy_single_pair_single_interval"
    )
    assert payload["supported_runtime_scope"]["multi_pair_portfolio_supported"] is False
    assert payload["supported_runtime_scope"]["multi_interval_runtime_supported"] is False
    assert list(payload) == sorted(payload)
