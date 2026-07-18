from __future__ import annotations

import copy
import sqlite3
from decimal import Decimal

import pytest

from market_research.research.corporate_action_contract import (
    AdjustmentPolicy,
    CorporateActionContractError,
    CorporateActionOhlcv,
    parse_corporate_action_set,
    transform_raw_ohlcv,
)
from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.dataset_snapshot import load_dataset_split
from market_research.research.market_calendar_contract import (
    MarketCalendarContractError,
    parse_market_calendar_authority,
)
from market_research.research.universe_contract import (
    UniverseContractError,
    parse_point_in_time_universe,
)
from market_research.research_composition import parse_builtin_manifest
from tests.test_instrument_domain_contracts import _manifest_with_domain_contracts


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _attribute(name: str, value: str) -> dict[str, str]:
    return {
        "name": name,
        "value": value,
        "value_type": "string",
        "unit": "classification",
    }


def _membership(
    *,
    membership_id: str,
    version_id: str,
    version: int,
    instrument_id: str,
    valid_to: str,
    status: str,
    observed_at: str,
    source_hash: str,
    supersedes: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "membership_id": membership_id,
        "membership_version_id": version_id,
        "version": version,
        "universe_id": "univ_research_demo_0001",
        "instrument_id": instrument_id,
        "valid_from": "2020-01-01",
        "valid_to": valid_to,
        "status": status,
        "published_at": observed_at,
        "observed_at": observed_at,
        "source_content_hash": source_hash,
        "attributes": [_attribute("sector", "digital_assets")],
        "supersedes_version_id": supersedes,
        "correction_reason": "provider corrected end date" if supersedes else None,
    }


def _universe_payload() -> dict[str, object]:
    first_version = "umv_btc_member_0001_v1"
    return {
        "schema_version": 1,
        "universe_id": "univ_research_demo_0001",
        "universe_version_id": "univv_research_demo_0001_v1",
        "version": 1,
        "name": "Offline reviewed universe",
        "source_uri": "/var/lib/market-research-inputs/universe-v1.json",
        "source_content_hash": _hash("a"),
        "source_schema_hash": _hash("b"),
        "prepared_at": "2024-01-01T00:00:00+00:00",
        "observed_at": "2024-01-01T00:01:00+00:00",
        "memberships": [
            _membership(
                membership_id="um_btc_member_0001",
                version_id=first_version,
                version=1,
                instrument_id="inst_btc_internal_0001",
                valid_to="2022-12-31",
                status="inactive",
                observed_at="2020-01-01T00:01:00+00:00",
                source_hash=_hash("c"),
            ),
            _membership(
                membership_id="um_btc_member_0001",
                version_id="umv_btc_member_0001_v2",
                version=2,
                instrument_id="inst_btc_internal_0001",
                valid_to="2021-12-31",
                status="inactive",
                observed_at="2023-02-01T00:00:00+00:00",
                source_hash=_hash("d"),
                supersedes=first_version,
            ),
            _membership(
                membership_id="um_eth_member_0001",
                version_id="umv_eth_member_0001_v1",
                version=1,
                instrument_id="inst_eth_internal_0001",
                valid_to="2021-06-30",
                status="delisted",
                observed_at="2020-01-02T00:00:00+00:00",
                source_hash=_hash("e"),
            ),
        ],
    }


def _session_calendar_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "calendar_id": "cal_xnys_sessions_0001",
        "calendar_version_id": "calv_xnys_sessions_0001_v1",
        "version": 1,
        "market_mode": "session",
        "timezone_name": "America/New_York",
        "tzdb_version": "2026a",
        "dst_transition_policy": (
            "iana_tzdb_reject_ambiguous_or_nonexistent_local_time"
        ),
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "source_uri": "/var/lib/market-research-inputs/xnys-calendar-2026.json",
        "source_content_hash": _hash("f"),
        "source_schema_hash": _hash("0"),
        "published_at": "2025-12-01T00:00:00+00:00",
        "observed_at": "2025-12-01T00:05:00+00:00",
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
                "exception_id": "calex_xnys_july3_2026",
                "local_date": "2026-07-03",
                "kind": "holiday",
                "reason": "reviewed market holiday",
                "published_at": "2026-05-01T00:00:00+00:00",
                "observed_at": "2026-06-01T00:00:00+00:00",
                "source_content_hash": _hash("1"),
                "close_local": None,
            },
            {
                "exception_id": "calex_xnys_nov27_2026",
                "local_date": "2026-11-27",
                "kind": "early_close",
                "reason": "reviewed early close",
                "published_at": "2026-05-01T00:00:00+00:00",
                "observed_at": "2026-06-01T00:00:00+00:00",
                "source_content_hash": _hash("2"),
                "close_local": "13:00",
            },
        ],
    }


def _event(
    *,
    event_id: str,
    version_id: str,
    event_type: str,
    effective_at: str,
    ratio: str | None = None,
    cash_amount: str | None = None,
    tradability: str | None = None,
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
        "source_content_hash": _hash("3"),
        "ratio": ratio,
        "cash_amount": cash_amount,
        "cash_currency": "KRW" if cash_amount is not None else None,
        "replacement_symbol": None,
        "replacement_instrument_id": None,
        "tradability": tradability,
    }


def _action_set(*, include_delisting: bool = False):
    events = [
        _event(
            event_id="ca_btc_split_0001",
            version_id="cav_btc_split_0001_v1",
            event_type="split",
            effective_at="2026-01-03T00:00:00+00:00",
            ratio="2",
        ),
        _event(
            event_id="ca_btc_dividend_0001",
            version_id="cav_btc_dividend_0001_v1",
            event_type="cash_dividend",
            effective_at="2026-01-04T00:00:00+00:00",
            cash_amount="5",
        ),
    ]
    if include_delisting:
        events.append(
            _event(
                event_id="ca_btc_delisted_0001",
                version_id="cav_btc_delisted_0001_v1",
                event_type="delisting",
                effective_at="2026-01-06T00:00:00+00:00",
                tradability="delisted",
            )
        )
    return parse_corporate_action_set(
        {
            "schema_version": 1,
            "instrument_id": "inst_btc_internal_0001",
            "action_set_id": "cas_btc_transform_0001",
            "events": events,
        },
        expected_instrument_id="inst_btc_internal_0001",
    )


def _rows() -> tuple[CorporateActionOhlcv, ...]:
    return (
        CorporateActionOhlcv(
            "2026-01-01T00:00:00+00:00",
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            Decimal("10"),
        ),
        CorporateActionOhlcv(
            "2026-01-02T00:00:00+00:00",
            Decimal("110"),
            Decimal("112"),
            Decimal("108"),
            Decimal("110"),
            Decimal("20"),
        ),
        CorporateActionOhlcv(
            "2026-01-03T00:00:00+00:00",
            Decimal("60"),
            Decimal("61"),
            Decimal("59"),
            Decimal("60"),
            Decimal("40"),
        ),
    )


def _adjusted_policy(action_set) -> AdjustmentPolicy:
    return AdjustmentPolicy(
        schema_version=1,
        policy_id="cap_total_return_0001",
        version=1,
        price_series="pre_adjusted",
        price_adjustment="backward_total_return",
        volume_adjustment="inverse_split_factor",
        dividend_treatment="included_in_total_return_adjustment",
        action_set_hash=action_set.contract_hash(),
    )


def test_point_in_time_universe_retains_history_without_future_correction_leakage():
    universe = parse_point_in_time_universe(_universe_payload())

    before_correction = universe.members_at(
        effective_on="2022-06-01", known_at="2022-06-01T00:00:00+00:00"
    )
    after_correction = universe.members_at(
        effective_on="2022-06-01", known_at="2024-01-01T00:00:00+00:00"
    )

    assert [item.version for item in before_correction] == [1]
    assert after_correction == ()
    assert {item.status for item in universe.memberships} == {"inactive", "delisted"}
    assert universe.evidence()["membership_version_count"] == 3
    assert universe.contract_hash().startswith("sha256:")


def test_universe_correction_chain_and_external_artifact_location_fail_closed():
    broken = _universe_payload()
    broken["memberships"][1]["supersedes_version_id"] = "umv_wrong_version_0001"
    with pytest.raises(UniverseContractError, match="correction_chain_broken"):
        parse_point_in_time_universe(broken)

    relative = _universe_payload()
    relative["source_uri"] = "inputs/universe.json"
    with pytest.raises(UniverseContractError, match="absolute_local_artifact"):
        parse_point_in_time_universe(relative)


def test_session_calendar_handles_dst_holiday_early_close_and_knowledge_time():
    calendar = parse_market_calendar_authority(_session_calendar_payload())
    known = "2026-12-01T00:00:00+00:00"

    before_dst = calendar.session_window(local_date="2026-03-02", known_at=known)
    after_dst = calendar.session_window(local_date="2026-03-09", known_at=known)
    assert before_dst is not None and before_dst.open_at_utc.endswith("14:30:00Z")
    assert after_dst is not None and after_dst.open_at_utc.endswith("13:30:00Z")

    assert calendar.session_window(local_date="2026-07-03", known_at=known) is None
    before_holiday_was_known = calendar.session_window(
        local_date="2026-07-03", known_at="2026-05-15T00:00:00+00:00"
    )
    assert before_holiday_was_known is not None

    early = calendar.session_window(local_date="2026-11-27", known_at=known)
    assert early is not None
    assert early.session_kind == "early_close"
    assert early.close_at_utc.endswith("18:00:00Z")
    assert not calendar.is_open_at(
        timestamp="2026-11-27T19:00:00+00:00", known_at=known
    )


def test_continuous_calendar_and_dst_ambiguous_session_fail_closed():
    payload = _session_calendar_payload()
    payload.update(
        {
            "calendar_id": "cal_continuous_0001",
            "calendar_version_id": "calv_continuous_0001_v1",
            "market_mode": "continuous_24x7",
            "timezone_name": "UTC",
            "weekly_sessions": [],
            "exceptions": [],
        }
    )
    continuous = parse_market_calendar_authority(payload)
    assert continuous.is_open_at(
        timestamp="2026-07-03T12:34:56+00:00",
        known_at="2026-07-03T12:34:56+00:00",
    )

    ambiguous = _session_calendar_payload()
    ambiguous["weekly_sessions"] = [
        {
            "weekday": 6,
            "open_local": "01:30",
            "close_local": "03:00",
            "close_day_offset": 0,
        }
    ]
    ambiguous["exceptions"] = []
    calendar = parse_market_calendar_authority(ambiguous)
    with pytest.raises(MarketCalendarContractError, match="ambiguous_local"):
        calendar.session_window(
            local_date="2026-11-01", known_at="2026-12-01T00:00:00+00:00"
        )


def test_raw_to_adjusted_corporate_action_evidence_binds_before_and_after_results():
    action_set = _action_set()
    result = transform_raw_ohlcv(
        _rows(),
        action_set=action_set,
        policy=_adjusted_policy(action_set),
        known_at="2026-01-05T00:00:00+00:00",
    )

    expected_dividend_factor = Decimal("55") / Decimal("60")
    assert result.rows[0].close == Decimal("50") * expected_dividend_factor
    assert result.rows[0].volume == Decimal("20")
    assert result.rows[2].close == Decimal("60") * expected_dividend_factor
    assert [item.event_type for item in result.applications] == [
        "split",
        "cash_dividend",
    ]
    assert all(
        item.rows_hash_before != item.rows_hash_after for item in result.applications
    )
    evidence = result.as_dict()
    assert evidence["input_rows_hash"] != evidence["output_rows_hash"]
    assert str(evidence["content_hash"]).startswith("sha256:")


@pytest.mark.parametrize("ratio", (Decimal("0.1"), Decimal("2"), Decimal("10")))
def test_split_adjustment_preserves_price_volume_product_property(ratio: Decimal):
    payload = {
        "schema_version": 1,
        "instrument_id": "inst_btc_internal_0001",
        "action_set_id": "cas_split_property_0001",
        "events": [
            _event(
                event_id="ca_split_property_0001",
                version_id="cav_split_property_0001_v1",
                event_type="split",
                effective_at="2026-01-03T00:00:00+00:00",
                ratio=str(ratio),
            )
        ],
    }
    action_set = parse_corporate_action_set(
        payload, expected_instrument_id="inst_btc_internal_0001"
    )
    policy = AdjustmentPolicy(
        schema_version=1,
        policy_id="cap_split_property_0001",
        version=1,
        price_series="pre_adjusted",
        price_adjustment="backward_split_only",
        volume_adjustment="inverse_split_factor",
        dividend_treatment="excluded",
        action_set_hash=action_set.contract_hash(),
    )
    original = _rows()
    result = transform_raw_ohlcv(
        original,
        action_set=action_set,
        policy=policy,
        known_at="2026-01-05T00:00:00+00:00",
    )
    for raw, adjusted in zip(original[:2], result.rows[:2]):
        assert raw.close * raw.volume == adjusted.close * adjusted.volume
    assert (
        result.as_dict()
        == transform_raw_ohlcv(
            original,
            action_set=action_set,
            policy=policy,
            known_at="2026-01-05T00:00:00+00:00",
        ).as_dict()
    )


def test_known_delisting_rejects_post_event_observations():
    action_set = _action_set(include_delisting=True)
    rows = (
        *_rows(),
        CorporateActionOhlcv(
            "2026-01-06T00:00:00+00:00",
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("0"),
        ),
    )
    with pytest.raises(CorporateActionContractError, match="post_delisting"):
        transform_raw_ohlcv(
            rows,
            action_set=action_set,
            policy=_adjusted_policy(action_set),
            known_at="2026-01-07T00:00:00+00:00",
        )


def test_manifest_hash_and_dataset_domain_evidence_bind_universe_and_calendar():
    payload = _manifest_with_domain_contracts()
    payload["universe"] = _universe_payload()
    payload["market_calendar"] = _session_calendar_payload()
    manifest = parse_builtin_manifest(payload)

    evidence = manifest.instrument_evidence()
    assert evidence["point_in_time_universe"]["universe_contract_hash"] == (
        manifest.universe.contract_hash()
    )
    assert evidence["market_calendar"]["calendar_contract_hash"] == (
        manifest.market_calendar.contract_hash()
    )
    assert manifest.canonical_payload()["universe"] == manifest.universe.as_dict()

    changed = copy.deepcopy(payload)
    changed["market_calendar"]["source_content_hash"] = _hash("9")
    assert parse_builtin_manifest(changed).manifest_hash() != manifest.manifest_hash()


def test_materialized_dataset_query_evidence_includes_universe_and_calendar_hashes(
    tmp_path,
):
    path = tmp_path / "candles.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE candles (pair TEXT NOT NULL, interval TEXT NOT NULL, "
            "ts INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
            "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("KRW-BTC", "1m", 1767225600000, 100.0, 101.0, 99.0, 100.0, 10.0),
        )
        conn.commit()
    finally:
        conn.close()

    payload = _manifest_with_domain_contracts()
    payload["universe"] = _universe_payload()
    payload["market_calendar"] = _session_calendar_payload()
    manifest = parse_builtin_manifest(payload)
    snapshot = load_dataset_split(db_path=path, manifest=manifest, split_name="train")
    domain = (snapshot.options or {})["domain_contracts"]

    assert domain["point_in_time_universe"]["universe_contract_hash"] == (
        manifest.universe.contract_hash()
    )
    assert domain["market_calendar"]["calendar_contract_hash"] == (
        manifest.market_calendar.contract_hash()
    )
    assert domain["corporate_actions"]["post_delisting_observation_policy"] == (
        "reject"
    )


def test_manifest_rejects_universe_without_selected_instrument_history():
    payload = _manifest_with_domain_contracts()
    universe = _universe_payload()
    for membership in universe["memberships"]:
        membership["instrument_id"] = "inst_eth_internal_0001"
    payload["universe"] = universe
    with pytest.raises(ManifestValidationError, match="instrument_missing"):
        parse_builtin_manifest(payload)
