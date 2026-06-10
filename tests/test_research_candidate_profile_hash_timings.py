from __future__ import annotations

import json
from pathlib import Path

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.research.promotion_gate import build_candidate_behavior_profile, build_candidate_profile
from tests.factories.research_reports import minimal_candidate_payload
from tests.test_research_backtest_reproducibility import (
    _create_db,
    _manifest,
    _production_bound_statistical_manifest,
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


def _run_statistical_report(tmp_path, monkeypatch, *, experiment_id: str) -> dict[str, object]:
    db_path = tmp_path / f"{experiment_id}.sqlite"
    _create_db(db_path)
    payload = _production_bound_statistical_manifest()
    payload["experiment_id"] = experiment_id
    payload["research_run"] = {"report_detail": "summary", "execution": {"mode": "serial"}}
    return _run_contract_research_backtest(
        manifest=parse_manifest(payload),
        db_path=db_path,
        manager=_research_manager(tmp_path, monkeypatch),
        generated_at="2026-05-03T00:00:00+00:00",
    )


def _persisted_timings(report: dict[str, object]) -> dict[str, dict[str, object]]:
    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))
    return {
        str(item["stage"]): item
        for item in persisted["execution_observability"]["stage_timings"]
    }


def test_candidate_profile_hash_records_profile_build_and_hash_timings(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_profile_profile_timing")

    timings = _persisted_timings(report)
    for stage in (
        "candidate_evaluation.candidate_profile_hash",
        "candidate_evaluation.candidate_profile_hash.profile_build",
        "candidate_evaluation.candidate_profile_hash.profile_hash",
    ):
        assert stage in timings
        assert timings[stage]["wall_seconds"] >= 0
        assert timings[stage]["candidate_count"] > 0
    assert timings["candidate_evaluation.candidate_profile_hash.profile_hash"]["hash_call_count"] > 0


def test_candidate_profile_hash_records_behavior_profile_build_and_hash_timings(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_profile_behavior_timing")

    timings = _persisted_timings(report)
    for stage in (
        "candidate_evaluation.candidate_profile_hash.behavior_profile_build",
        "candidate_evaluation.candidate_profile_hash.behavior_profile_hash",
    ):
        assert stage in timings
        assert timings[stage]["wall_seconds"] >= 0
        assert timings[stage]["candidate_count"] > 0
    assert timings["candidate_evaluation.candidate_profile_hash.behavior_profile_hash"]["hash_call_count"] > 0


def test_hot_path_builds_candidate_profile_once_per_candidate(tmp_path, monkeypatch) -> None:
    from bithumb_bot.research import validation_protocol

    calls = 0
    original = validation_protocol.build_candidate_profile

    def counted(candidate):
        nonlocal calls
        calls += 1
        return original(candidate)

    monkeypatch.setattr(validation_protocol, "build_candidate_profile", counted)
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_profile_once")
    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))

    assert calls == persisted["candidate_count"]


def test_behavior_profile_accepts_shared_base_profile() -> None:
    candidate = minimal_candidate_payload()
    base_profile = build_candidate_profile(candidate)

    assert build_candidate_behavior_profile(candidate, base_profile=base_profile) == build_candidate_behavior_profile(
        candidate
    )
    assert sha256_prefixed(build_candidate_behavior_profile(candidate, base_profile=base_profile)).startswith("sha256:")


def test_post_statistical_candidate_profile_rehash_records_timing(tmp_path, monkeypatch) -> None:
    report = _run_statistical_report(
        tmp_path,
        monkeypatch,
        experiment_id="post_statistical_candidate_profile_hash_timing",
    )

    timings = _persisted_timings(report)
    for stage in (
        "candidate_profile_hash.profile_build",
        "candidate_profile_hash.profile_hash",
        "candidate_profile_hash.behavior_profile_build",
        "candidate_profile_hash.behavior_profile_hash",
        "candidate_profile_hash.post_statistical_profile_build",
        "candidate_profile_hash.post_statistical_profile_hash",
    ):
        assert stage in timings
        assert timings[stage]["wall_seconds"] >= 0
        assert timings[stage]["candidate_count"] > 0

    post_hash = timings["candidate_profile_hash.post_statistical_profile_hash"]
    assert post_hash["hash_call_count"] > 0
    assert post_hash["observed_hash_payload_bytes"] > 0
    assert post_hash["largest_hash_payload_bytes"] > 0
    assert post_hash["largest_hash_label"] == "candidate_profile_hash.post_statistical_profile_hash"
