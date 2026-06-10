from __future__ import annotations

import json
from pathlib import Path

from bithumb_bot.research.experiment_manifest import parse_manifest
from tests.factories.research_reports import minimal_candidate_payload, minimal_scenario_result
from tests.test_research_backtest_reproducibility import (
    _create_db,
    _manifest,
    _research_manager,
    _run_contract_research_backtest,
)


def _run_report(tmp_path, monkeypatch, *, experiment_id: str) -> dict[str, object]:
    db_path = tmp_path / f"{experiment_id}.sqlite"
    _create_db(db_path)
    payload = _manifest()
    payload["experiment_id"] = experiment_id
    payload["research_run"] = {"report_detail": "summary", "execution": {"mode": "serial"}}
    return _run_contract_research_backtest(
        manifest=parse_manifest(payload),
        db_path=db_path,
        manager=_research_manager(tmp_path, monkeypatch),
        generated_at="2026-05-03T00:00:00+00:00",
    )


def _observability(report: dict[str, object]) -> dict[str, object]:
    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))
    return persisted["execution_observability"]["candidate_profile_hash_observability"]


def test_candidate_profile_hash_records_hash_call_count(tmp_path, monkeypatch) -> None:
    obs = _observability(_run_report(tmp_path, monkeypatch, experiment_id="candidate_profile_hash_calls"))

    assert obs["candidate_count"] > 0
    assert obs["hash_call_count"] >= obs["candidate_count"] * 2
    assert obs["profile_hash"]["hash_call_count"] == obs["candidate_count"]
    assert obs["behavior_profile_hash"]["hash_call_count"] == obs["candidate_count"]


def test_candidate_profile_hash_records_observed_hash_payload_bytes(tmp_path, monkeypatch) -> None:
    obs = _observability(_run_report(tmp_path, monkeypatch, experiment_id="candidate_profile_hash_bytes"))

    assert obs["observed_hash_payload_bytes"] > 0
    assert obs["profile_hash"]["observed_hash_payload_bytes"] > 0
    assert obs["behavior_profile_hash"]["observed_hash_payload_bytes"] > 0
    assert obs["largest_hash_payload_bytes"] > 0


def test_candidate_profile_hash_records_largest_hash_label(tmp_path, monkeypatch) -> None:
    obs = _observability(_run_report(tmp_path, monkeypatch, experiment_id="candidate_profile_hash_label"))

    assert obs["largest_hash_label"] in {"candidate_profile_hash", "candidate_behavior_profile_hash"}


def test_candidate_profile_hash_bytes_stay_bounded_for_large_payload() -> None:
    from bithumb_bot.research.hashing import observe_hashing, sha256_prefixed
    from bithumb_bot.research.promotion_gate import build_candidate_behavior_profile, build_candidate_profile

    large_trace = [{"step": index, "payload": "x" * 100} for index in range(2_000)]
    candidate = minimal_candidate_payload(
        strategy_diagnostics={"large": ["diagnostic"] * 1_000},
        market_regime_bucket_performance=[{"bucket": index, "payload": "y" * 100} for index in range(1_000)],
        scenario_results=[
            minimal_scenario_result(
                train_resource_usage={"stage_trace": large_trace, "candles_processed": 10},
                validation_resource_usage={"stage_trace": large_trace, "candles_processed": 10},
            )
        ],
    )
    profile = build_candidate_profile(candidate)
    behavior_profile = build_candidate_behavior_profile(candidate, base_profile=profile)

    with observe_hashing() as observer:
        sha256_prefixed(profile, label="candidate_profile_hash")
        sha256_prefixed(behavior_profile, label="candidate_behavior_profile_hash")

    assert observer.observed_hash_payload_bytes < 80_000
