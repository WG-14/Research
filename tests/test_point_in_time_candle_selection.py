from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_research.research.corporate_action_contract import (
    AdjustmentPolicy,
    parse_corporate_action_set,
)
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import DateRange
from market_research.research.hashing import sha256_prefixed
from market_research.research.instrument_contract import parse_instrument_master
from market_research.research.market_calendar_contract import (
    parse_market_calendar_authority,
)
from market_research.research.point_in_time_selection import (
    PointInTimeSelectionError,
    build_point_in_time_decision_evidence,
    point_in_time_execution_snapshot,
    require_point_in_time_scope,
    verify_point_in_time_decision_evidence,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.universe_contract import parse_point_in_time_universe
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_instrument_domain_contracts import _instrument


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _membership(
    *,
    version: int = 1,
    valid_to: str = "2026-12-31",
    status: str = "active",
    observed_at: str = "2025-12-01T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "membership_id": "um_btc_selection_0001",
        "membership_version_id": f"umv_btc_selection_0001_v{version}",
        "version": version,
        "universe_id": "univ_selection_test_0001",
        "instrument_id": "inst_btc_internal_0001",
        "valid_from": "2026-01-01",
        "valid_to": valid_to,
        "status": status,
        "published_at": observed_at,
        "observed_at": observed_at,
        "source_content_hash": _hash(str(version)),
        "attributes": [],
        "supersedes_version_id": (
            "umv_btc_selection_0001_v1" if version == 2 else None
        ),
        "correction_reason": "reviewed end-date correction" if version == 2 else None,
    }


def _universe(
    source_uri: str,
    source_hash: str,
    *,
    future_correction: bool = False,
    inactive_valid_to: str | None = None,
):
    memberships = [
        _membership(
            valid_to=inactive_valid_to or "2026-12-31",
            status="inactive" if inactive_valid_to else "active",
        )
    ]
    if future_correction:
        memberships.append(
            _membership(
                version=2,
                valid_to="2026-06-30",
                status="inactive",
                observed_at="2026-08-01T00:00:00+00:00",
            )
        )
    return parse_point_in_time_universe(
        {
            "schema_version": 1,
            "universe_id": "univ_selection_test_0001",
            "universe_version_id": "univv_selection_test_0001_v1",
            "version": 1,
            "name": "Frozen selection test universe",
            "source_uri": source_uri,
            "source_content_hash": source_hash,
            "source_schema_hash": _hash("a"),
            "prepared_at": "2026-12-31T00:00:00+00:00",
            "observed_at": "2026-12-31T00:00:00+00:00",
            "memberships": memberships,
        }
    )


def _calendar(source_uri: str, source_hash: str):
    return parse_market_calendar_authority(
        {
            "schema_version": 1,
            "calendar_id": "cal_selection_test_0001",
            "calendar_version_id": "calv_selection_test_0001_v1",
            "version": 1,
            "market_mode": "session",
            "timezone_name": "America/New_York",
            "tzdb_version": "2026a",
            "dst_transition_policy": (
                "iana_tzdb_reject_ambiguous_or_nonexistent_local_time"
            ),
            "valid_from": "2026-01-01",
            "valid_to": "2026-12-31",
            "source_uri": source_uri,
            "source_content_hash": source_hash,
            "source_schema_hash": _hash("b"),
            "published_at": "2025-12-01T00:00:00+00:00",
            "observed_at": "2025-12-01T00:00:00+00:00",
            "weekly_sessions": [
                {
                    "weekday": weekday,
                    "open_local": "09:30",
                    "close_local": "16:00",
                    "close_day_offset": 0,
                }
                for weekday in range(5)
            ],
            "exceptions": [
                {
                    "exception_id": "calex_selection_july3_2026",
                    "local_date": "2026-07-03",
                    "kind": "holiday",
                    "reason": "reviewed holiday",
                    "published_at": "2026-05-01T00:00:00+00:00",
                    "observed_at": "2026-06-01T00:00:00+00:00",
                    "source_content_hash": _hash("c"),
                    "close_local": None,
                },
                {
                    "exception_id": "calex_selection_nov27_2026",
                    "local_date": "2026-11-27",
                    "kind": "early_close",
                    "reason": "reviewed early close",
                    "published_at": "2026-05-01T00:00:00+00:00",
                    "observed_at": "2026-06-01T00:00:00+00:00",
                    "source_content_hash": _hash("9"),
                    "close_local": "13:00",
                },
            ],
        }
    )


def _event(
    *,
    event_id: str,
    version_id: str,
    event_type: str,
    effective_at: str,
    tradability: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_version_id": version_id,
        "version": 1,
        "instrument_id": "inst_btc_internal_0001",
        "event_type": event_type,
        "effective_at": effective_at,
        "published_at": effective_at,
        "observed_at": effective_at,
        "source_content_hash": _hash("d"),
        "ratio": None,
        "cash_amount": None,
        "cash_currency": None,
        "replacement_symbol": None,
        "replacement_instrument_id": None,
        "tradability": tradability,
    }


def _action_set(*, delist: bool = False, future_halt: bool = False):
    events = [
        _event(
            event_id="ca_selection_halt_0001",
            version_id="cav_selection_halt_0001_v1",
            event_type="trading_halt",
            effective_at="2026-07-02T15:00:00+00:00",
            tradability="halted",
        ),
        _event(
            event_id="ca_selection_resume_0001",
            version_id="cav_selection_resume_0001_v1",
            event_type="trading_resume",
            effective_at="2026-07-02T16:00:00+00:00",
            tradability="tradable",
        ),
    ]
    if delist:
        events.append(
            _event(
                event_id="ca_selection_delist_0001",
                version_id="cav_selection_delist_0001_v1",
                event_type="delisting",
                effective_at="2026-07-06T14:00:00+00:00",
                tradability="delisted",
            )
        )
    if future_halt:
        events.append(
            _event(
                event_id="ca_selection_future_0001",
                version_id="cav_selection_future_0001_v1",
                event_type="trading_halt",
                effective_at="2026-09-01T15:00:00+00:00",
                tradability="halted",
            )
        )
    return parse_corporate_action_set(
        {
            "schema_version": 1,
            "instrument_id": "inst_btc_internal_0001",
            "action_set_id": "cas_selection_test_0001",
            "events": events,
        },
        expected_instrument_id="inst_btc_internal_0001",
    )


def _manifest(
    *,
    universe,
    calendar,
    classification: str = "research_only",
    actions=None,
):
    action_set = actions or _action_set()
    return SimpleNamespace(
        research_classification=classification,
        instrument=parse_instrument_master(_instrument()),
        universe=universe,
        market_calendar=calendar,
        corporate_action_set=action_set,
        corporate_action_policy=AdjustmentPolicy(
            schema_version=1,
            policy_id="cap_selection_raw_0001",
            version=1,
            price_series="raw",
            price_adjustment="none",
            volume_adjustment="none",
            dividend_treatment="cash_flow_separate",
            action_set_hash=action_set.contract_hash(),
        ),
        execution_timing=SimpleNamespace(decision_guard_ms=0),
    )


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _snapshot(manifest, *timestamps: str) -> DatasetSnapshot:
    candles = tuple(
        Candle(_ts(value), 100.0, 101.0, 99.0, 100.0, 1.0) for value in timestamps
    )
    options = {
        "domain_contracts": {
            "instrument": {
                "instrument_contract_hash": manifest.instrument.contract_hash()
            },
            "point_in_time_universe": {
                "universe_contract_hash": manifest.universe.contract_hash()
            },
            "market_calendar": {
                "calendar_contract_hash": manifest.market_calendar.contract_hash()
            },
            "corporate_actions": {
                "action_set_hash": manifest.corporate_action_set.contract_hash()
            },
        }
    }
    return DatasetSnapshot(
        snapshot_id="pit-test",
        source="offline_fixture",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange("2026-07-01", "2026-12-31"),
        candles=candles,
        options=options,
    )


def _default_authorities():
    return (
        _universe("/nonexistent/pit-universe.json", _hash("e")),
        _calendar("/nonexistent/pit-calendar.json", _hash("f")),
    )


def test_selection_consumes_holiday_early_close_halt_and_resume_authorities() -> None:
    universe, calendar = _default_authorities()
    manifest = _manifest(universe=universe, calendar=calendar)
    snapshot = _snapshot(
        manifest,
        "2026-07-02T14:00:00+00:00",
        "2026-07-02T15:01:00+00:00",
        "2026-07-02T16:01:00+00:00",
        "2026-07-03T14:00:00+00:00",
        "2026-11-27T18:00:00+00:00",
    )
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    assert evidence is not None
    rows = evidence["rows"]
    assert [row["selected"] for row in rows] == [True, False, True, False, False]
    assert "corporate_action_trading_halt" in rows[1]["reasons"]
    assert "market_calendar_closed" in rows[3]["reasons"]
    assert rows[3]["calendar_exception"]["kind"] == "holiday"
    assert rows[4]["calendar_exception"]["kind"] == "early_close"

    bound = replace(snapshot, point_in_time_decision_evidence=evidence)
    selected, verified = point_in_time_execution_snapshot(
        snapshot=bound, expected_decision_guard_ms=0
    )
    assert verified is not None
    assert [item.ts for item in selected.candles] == [
        snapshot.candles[0].ts,
        snapshot.candles[2].ts,
    ]

    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("noop_baseline"),
        dataset=bound,
        parameter_values={},
        fee_rate=0.001,
        slippage_bps=10,
    )
    assert run.candle_count == 2
    assert run.point_in_time_decision_stream_hash == evidence["decision_stream_hash"]
    assert len(run.point_in_time_decision_evidence) == len(snapshot.candles)
    assert (
        run.execution_event_summary["point_in_time_authority_binding_hash"]
        == evidence["authority_binding_hash"]
    )


def test_inactive_history_is_kept_before_end_and_delisting_is_fail_closed() -> None:
    _, calendar = _default_authorities()
    universe = _universe(
        "/nonexistent/pit-universe.json",
        _hash("e"),
        inactive_valid_to="2026-07-02",
    )
    manifest = _manifest(
        universe=universe, calendar=calendar, actions=_action_set(delist=True)
    )
    snapshot = _snapshot(
        manifest,
        "2026-07-02T14:00:00+00:00",
        "2026-07-06T14:01:00+00:00",
    )
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    rows = evidence["rows"]
    assert rows[0]["selected"] is True
    assert rows[0]["selected_membership"]["status"] == "inactive"
    assert rows[1]["selected"] is False
    assert "universe_membership_not_effective" in rows[1]["reasons"]
    assert "corporate_action_delisted" in rows[1]["reasons"]


def test_future_corrections_and_future_action_suffix_do_not_change_prior_row() -> None:
    _, calendar = _default_authorities()
    base_universe = _universe("/nonexistent/base-universe.json", _hash("1"))
    extended_universe = _universe(
        "/nonexistent/extended-universe.json",
        _hash("2"),
        future_correction=True,
    )
    base = _manifest(universe=base_universe, calendar=calendar)
    extended = _manifest(
        universe=extended_universe,
        calendar=calendar,
        actions=_action_set(future_halt=True),
    )
    base_snapshot = _snapshot(base, "2026-07-02T14:00:00+00:00")
    extended_snapshot = _snapshot(extended, "2026-07-02T14:00:00+00:00")
    base_evidence = build_point_in_time_decision_evidence(
        manifest=base, snapshot=base_snapshot
    )
    extended_evidence = build_point_in_time_decision_evidence(
        manifest=extended, snapshot=extended_snapshot
    )
    assert base_evidence["rows"] == extended_evidence["rows"]
    assert (
        base_evidence["authority_binding_hash"]
        != (extended_evidence["authority_binding_hash"])
    )

    later_snapshot = _snapshot(extended, "2026-09-02T14:00:00+00:00")
    later = build_point_in_time_decision_evidence(
        manifest=extended, snapshot=later_snapshot
    )
    assert later["rows"][0]["selected"] is False
    assert "universe_membership_not_effective" in later["rows"][0]["reasons"]
    assert "corporate_action_trading_halt" in later["rows"][0]["reasons"]


def test_known_future_effective_action_correction_supersedes_old_version() -> None:
    first = _event(
        event_id="ca_selection_corrected_0001",
        version_id="cav_selection_corrected_0001_v1",
        event_type="trading_halt",
        effective_at="2026-07-02T15:00:00+00:00",
        tradability="halted",
    )
    correction = copy.deepcopy(first)
    correction.update(
        {
            "event_version_id": "cav_selection_corrected_0001_v2",
            "version": 2,
            "effective_at": "2026-09-01T15:00:00+00:00",
            "published_at": "2026-08-01T00:00:00+00:00",
            "observed_at": "2026-08-01T00:00:00+00:00",
        }
    )
    action_set = parse_corporate_action_set(
        {
            "schema_version": 1,
            "instrument_id": "inst_btc_internal_0001",
            "action_set_id": "cas_selection_corrected_0001",
            "events": [first, correction],
        },
        expected_instrument_id="inst_btc_internal_0001",
    )

    assert (
        action_set.latest_effective_and_known(as_of="2026-08-15T00:00:00+00:00") == ()
    )
    assert (
        action_set.latest_effective_and_known(as_of="2026-09-02T00:00:00+00:00")[
            0
        ].event_version_id
        == "cav_selection_corrected_0001_v2"
    )


def test_validation_scope_rejects_missing_authority_and_source_tamper(
    tmp_path: Path,
) -> None:
    universe_source = tmp_path / "universe-source.json"
    calendar_source = tmp_path / "calendar-source.json"
    universe_source.write_bytes(b"immutable-universe-source-v1")
    calendar_source.write_bytes(b"immutable-calendar-source-v1")
    universe = _universe(str(universe_source), _file_hash(universe_source))
    calendar = _calendar(str(calendar_source), _file_hash(calendar_source))
    manifest = _manifest(
        universe=universe,
        calendar=calendar,
        classification="validated_candidate",
    )
    binding = require_point_in_time_scope(manifest, verify_source_content=True)
    assert (
        binding["authorities"]["source_content_verification"]["point_in_time_universe"][
            "status"
        ]
        == "VERIFIED"
    )
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest,
        snapshot=_snapshot(manifest, "2026-07-02T14:00:00+00:00"),
    )
    assert evidence["selected_candle_count"] == 1

    missing = copy.copy(manifest)
    missing.market_calendar = None
    with pytest.raises(
        PointInTimeSelectionError, match="scope_missing:market_calendar"
    ):
        require_point_in_time_scope(missing, verify_source_content=True)

    universe_source.write_bytes(b"tampered")
    with pytest.raises(PointInTimeSelectionError, match="content_hash_mismatch"):
        require_point_in_time_scope(manifest, verify_source_content=True)


def test_point_in_time_evidence_tamper_is_rejected() -> None:
    universe, calendar = _default_authorities()
    manifest = _manifest(universe=universe, calendar=calendar)
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    bound = replace(snapshot, point_in_time_decision_evidence=evidence)
    assert verify_point_in_time_decision_evidence(snapshot=bound) is not None

    tampered = copy.deepcopy(evidence)
    tampered["rows"][0]["selected"] = False
    unhashed = dict(tampered)
    unhashed.pop("content_hash")
    tampered["content_hash"] = sha256_prefixed(
        unhashed, label="point_in_time_decision_evidence"
    )
    with pytest.raises(PointInTimeSelectionError, match="row_hash_mismatch"):
        verify_point_in_time_decision_evidence(
            snapshot=replace(snapshot, point_in_time_decision_evidence=tampered)
        )
