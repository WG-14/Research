from __future__ import annotations

from bithumb_bot.research.backtest_engine import BacktestRunContext
from tests.test_research_backtest_observability_policy import _run


def test_empty_and_invariant_hashes_are_precomputed_outside_tick_loop(monkeypatch) -> None:
    from bithumb_bot.research import backtest_stage_runner

    labels: list[str] = []
    real_hash = backtest_stage_runner.canonical_payload_hash

    def spy(value, *, label="canonical_payload"):  # type: ignore[no-untyped-def]
        labels.append(str(label))
        return real_hash(value, label=label)

    monkeypatch.setattr(backtest_stage_runner, "canonical_payload_hash", spy)

    _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=6)

    assert labels.count("invariant_empty_fill") == 1
    assert labels.count("invariant_empty_order_rules") == 1
    assert labels.count("invariant_execution_timing_policy") == 1


def test_tick_variant_hashes_still_change_with_tick_state() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=4)
    replay_hashes = [
        trace["payload"]["replay_tick_hash"]
        for trace in result.resource_usage["stage_trace"]
        if trace["stage_id"] == "strategy"
    ]

    assert len(replay_hashes) == 4
    assert len(set(replay_hashes)) == 4


def test_full_canonical_reuses_invariant_hashes_outside_tick_loop(monkeypatch) -> None:
    from bithumb_bot.research import backtest_common, backtest_stage_runner

    labels: list[str] = []
    real_stage_hash = backtest_stage_runner.canonical_payload_hash
    real_common_hash = backtest_common.canonical_payload_hash

    def stage_spy(value, *, label="canonical_payload"):  # type: ignore[no-untyped-def]
        labels.append(str(label))
        return real_stage_hash(value, label=label)

    def common_spy(value, *, label="canonical_payload"):  # type: ignore[no-untyped-def]
        labels.append(str(label))
        return real_common_hash(value, label=label)

    monkeypatch.setattr(backtest_stage_runner, "canonical_payload_hash", stage_spy)
    monkeypatch.setattr(backtest_common, "canonical_payload_hash", common_spy)

    result = _run(context=BacktestRunContext(report_detail="full"), count=5)

    assert result.resource_usage["canonical_evidence_policy"] == "full_tick_canonical"
    assert labels.count("invariant_execution_timing_policy") == 1
    assert labels.count("invariant_fee_model") == 1
    assert labels.count("invariant_slippage_model") == 1
    assert labels.count("invariant_parameter_values") == 1
    assert labels.count("invariant_candidate_profile") == 1
    assert labels.count("full_payload_execution_timing_policy_fallback") == 0
    assert labels.count("full_payload_fee_model_fallback") == 0
    assert labels.count("full_payload_slippage_model_fallback") == 0
    assert labels.count("full_payload_parameter_values_fallback") == 0
    assert labels.count("full_payload_candidate_profile_fallback") == 0
    assert isinstance(result.decisions[0]["strategy_spec"], dict)
    assert isinstance(result.decisions[0]["strategy_plugin_contract"], dict)
    assert result.decisions[0]["candidate_profile_hash"].startswith("sha256:")


def test_full_canonical_keeps_tick_variant_hashes_per_tick(monkeypatch) -> None:
    from bithumb_bot.research import backtest_common

    labels: list[tuple[str, str]] = []
    real_common_hash = backtest_common.canonical_payload_hash

    def common_spy(value, *, label="canonical_payload"):  # type: ignore[no-untyped-def]
        digest = real_common_hash(value, label=label)
        labels.append((str(label), digest))
        return digest

    monkeypatch.setattr(backtest_common, "canonical_payload_hash", common_spy)

    result = _run(context=BacktestRunContext(report_detail="full"), count=4)
    replay_hashes = [
        trace["payload"]["replay_tick_hash"]
        for trace in result.resource_usage["stage_trace"]
        if trace["stage_id"] == "strategy"
    ]
    exit_evaluation_hashes = [
        digest for label, digest in labels if label == "canonical_payload"
    ]

    assert len(replay_hashes) == 4
    assert len(set(replay_hashes)) == 4
    assert len(exit_evaluation_hashes) >= 4
    assert len(set(exit_evaluation_hashes)) > 1
