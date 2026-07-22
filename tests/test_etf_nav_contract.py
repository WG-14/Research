from __future__ import annotations

import copy
import sqlite3
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from market_research.research.dataset_snapshot import load_dataset_split
from market_research.research.etf_nav_contract import (
    EtfNavContractError,
    parse_etf_nav_history,
)
from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.research_package_registry import _project_target_asset
from market_research.research_composition import parse_builtin_manifest
from tests.test_instrument_domain_contracts import _manifest_with_domain_contracts
from tests.test_point_in_time_domain_contracts import (
    _session_calendar_payload,
    _universe_payload,
)


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _record(
    *,
    nav_id: str,
    nav_type: str,
    revision: int = 1,
    nav_version_id: str | None = None,
    valuation_at: str = "2026-01-01T16:00:00+00:00",
    published_at: str = "2026-01-01T16:01:00+00:00",
    provider_received_at: str = "2026-01-01T16:02:00+00:00",
    system_received_at: str = "2026-01-01T16:03:00+00:00",
    processed_at: str = "2026-01-01T16:04:00+00:00",
    nav_per_share: object = "100",
    market_price: object = "101",
    premium_discount: object = "0.01",
    supersedes_version_id: str | None = None,
    correction_reason: str | None = None,
) -> dict[str, object]:
    version_id = nav_version_id or f"navv_{nav_id.removeprefix('nav_')}_v{revision}"
    return {
        "schema_version": 1,
        "nav_id": nav_id,
        "nav_version_id": version_id,
        "revision": revision,
        "instrument_id": "inst_btc_internal_0001",
        "underlying_index_id": "index_research_krw_btc_v1",
        "underlying_index_content_hash": _hash("1"),
        "nav_type": nav_type,
        "valuation_at": valuation_at,
        "published_at": published_at,
        "provider_received_at": provider_received_at,
        "system_received_at": system_received_at,
        "processed_at": processed_at,
        "currency": "KRW",
        "nav_per_share": nav_per_share,
        "market_price_ref": {
            "reference_id": f"navpx_{nav_id.removeprefix('nav_')}_v{revision}",
            "instrument_id": "inst_btc_internal_0001",
            "valuation_at": valuation_at,
            "available_at": "2026-01-01T16:02:30+00:00",
            "currency": "KRW",
            "price_per_share": market_price,
            "source_content_hash": _hash("2"),
        },
        "premium_discount": premium_discount,
        "source_content_hash": _hash("3" if revision == 1 else "4"),
        "supersedes_version_id": supersedes_version_id,
        "correction_reason": correction_reason,
    }


def _history_payload(*, include_correction: bool = True) -> dict[str, object]:
    inav = _record(
        nav_id="nav_inav_demo_0001",
        nav_type="inav",
        valuation_at="2026-01-01T15:59:00+00:00",
        published_at="2026-01-01T15:59:05+00:00",
        provider_received_at="2026-01-01T15:59:10+00:00",
        system_received_at="2026-01-01T15:59:15+00:00",
        processed_at="2026-01-01T15:59:20+00:00",
        nav_per_share="100",
        market_price="100",
        premium_discount="0",
    )
    inav_price = inav["market_price_ref"]
    assert isinstance(inav_price, dict)
    inav_price["available_at"] = "2026-01-01T15:59:12+00:00"
    official_v1 = _record(nav_id="nav_official_demo_0001", nav_type="official_nav")
    records = [inav, official_v1]
    if include_correction:
        records.append(
            _record(
                nav_id="nav_official_demo_0001",
                nav_type="official_nav",
                revision=2,
                published_at="2026-01-01T17:00:00+00:00",
                provider_received_at="2026-01-01T17:01:00+00:00",
                system_received_at="2026-01-01T17:02:00+00:00",
                processed_at="2026-01-01T17:03:00+00:00",
                market_price="102",
                premium_discount="0.02",
                supersedes_version_id="navv_official_demo_0001_v1",
                correction_reason="provider corrected the market-price reference",
            )
        )
        correction_price = records[-1]["market_price_ref"]
        assert isinstance(correction_price, dict)
        correction_price["available_at"] = "2026-01-01T16:02:30+00:00"
    return {
        "schema_version": 1,
        "authority_id": "etfnav_research_demo_0001",
        "authority_version_id": "etfnavv_research_demo_0001_v1",
        "version": 1,
        "instrument_id": "inst_btc_internal_0001",
        "underlying_index_id": "index_research_krw_btc_v1",
        "underlying_index_content_hash": _hash("1"),
        "currency": "KRW",
        "source_uri": "/var/lib/market-research-inputs/etf-nav-v1.json",
        "source_manifest_hash": _hash("5"),
        "source_content_hash": _hash("6"),
        "source_schema_hash": _hash("7"),
        "prepared_at": "2026-01-01T18:00:00+00:00",
        "records": records,
    }


def _etf_manifest_payload(*, include_nav: bool = True) -> dict[str, object]:
    payload = _manifest_with_domain_contracts()
    instrument = payload["instrument"]
    assert isinstance(instrument, dict)
    instrument["asset_type"] = "etf"
    instrument["etf_underlying_index_id"] = "index_research_krw_btc_v1"
    if include_nav:
        payload["etf_nav"] = _history_payload()
    return payload


def _write_candles(path: Path, *, ts: int = 1767225600000) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE candles (pair TEXT NOT NULL, interval TEXT NOT NULL, "
            "ts INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
            "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("KRW-BTC", "1m", ts, 100, 101, 99, 100, 10),
        )
        conn.commit()
    finally:
        conn.close()


def test_etf_nav_retains_nav_types_timelines_hashes_and_exact_premium() -> None:
    history = parse_etf_nav_history(_history_payload())

    assert history.instrument_id == "inst_btc_internal_0001"
    assert history.underlying_index_content_hash == _hash("1")
    assert {item.nav_type for item in history.records} == {"official_nav", "inav"}
    assert history.records[1].premium_discount == Decimal("0.01")
    assert history.records[1].market_price_ref.available_at.endswith("+00:00")
    assert history.contract_hash().startswith("sha256:")
    assert history.evidence()["source_manifest_hash"] == _hash("5")
    with pytest.raises(FrozenInstanceError):
        history.currency = "USD"  # type: ignore[misc]


def test_etf_nav_as_of_resolver_does_not_leak_future_correction() -> None:
    history = parse_etf_nav_history(_history_payload())

    before = history.resolve_as_of(
        known_at="2026-01-01T16:30:00+00:00", nav_type="official_nav"
    )
    after = history.resolve_as_of(
        known_at="2026-01-01T17:30:00+00:00", nav_type="official_nav"
    )
    assert before.revision == 1
    assert before.market_price_ref.price_per_share == Decimal("101")
    assert after.revision == 2
    assert after.market_price_ref.price_per_share == Decimal("102")
    equivalent_timestamp = history.resolve_as_of(
        known_at="2026-01-01T17:30:00Z",
        nav_type="official_nav",
        valuation_at="2026-01-01T16:00:00Z",
    )
    assert equivalent_timestamp.revision == 2
    with pytest.raises(EtfNavContractError, match="no_record_known_at"):
        history.resolve_as_of(
            known_at="2026-01-01T15:00:00+00:00", nav_type="official_nav"
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("nav_per_share", 100.0, "decimal_string"),
        ("nav_per_share", "NaN", "non_finite"),
        ("source_content_hash", "sha256:not-a-hash", "hash_invalid"),
    ],
)
def test_etf_nav_rejects_float_nonfinite_and_bad_hash(
    field: str, value: object, message: str
) -> None:
    payload = _history_payload(include_correction=False)
    records = payload["records"]
    assert isinstance(records, list)
    records[1][field] = value
    with pytest.raises(EtfNavContractError, match=message):
        parse_etf_nav_history(payload)


def test_etf_nav_rejects_unknown_fields_and_forged_computed_premium() -> None:
    unknown = _history_payload(include_correction=False)
    unknown["legacy_nav"] = "100"
    with pytest.raises(EtfNavContractError, match="unknown_fields:legacy_nav"):
        parse_etf_nav_history(unknown)

    forged = _history_payload(include_correction=False)
    records = forged["records"]
    assert isinstance(records, list)
    records[1]["premium_discount"] = "0.50"
    with pytest.raises(EtfNavContractError, match="premium_discount_mismatch"):
        parse_etf_nav_history(forged)


def test_etf_nav_rejects_time_and_market_reference_misalignment() -> None:
    misaligned = _history_payload(include_correction=False)
    records = misaligned["records"]
    assert isinstance(records, list)
    price_ref = records[1]["market_price_ref"]
    assert isinstance(price_ref, dict)
    price_ref["valuation_at"] = "2026-01-01T15:59:59+00:00"
    with pytest.raises(EtfNavContractError, match="market_price_time_misaligned"):
        parse_etf_nav_history(misaligned)

    future_price = _history_payload(include_correction=False)
    future_records = future_price["records"]
    assert isinstance(future_records, list)
    future_ref = future_records[1]["market_price_ref"]
    assert isinstance(future_ref, dict)
    future_ref["available_at"] = "2026-01-01T16:03:30+00:00"
    with pytest.raises(EtfNavContractError, match="not_available_when_received"):
        parse_etf_nav_history(future_price)

    reversed_times = _history_payload(include_correction=False)
    reversed_records = reversed_times["records"]
    assert isinstance(reversed_records, list)
    reversed_records[1]["provider_received_at"] = "2026-01-01T15:00:00+00:00"
    with pytest.raises(EtfNavContractError, match="time_order_invalid"):
        parse_etf_nav_history(reversed_times)


def test_etf_nav_rejects_wrong_identity_duplicate_and_broken_revision() -> None:
    wrong_instrument = _history_payload(include_correction=False)
    records = wrong_instrument["records"]
    assert isinstance(records, list)
    records[1]["instrument_id"] = "inst_wrong_internal_0001"
    wrong_price_ref = records[1]["market_price_ref"]
    assert isinstance(wrong_price_ref, dict)
    wrong_price_ref["instrument_id"] = "inst_wrong_internal_0001"
    with pytest.raises(EtfNavContractError, match="record.instrument_mismatch"):
        parse_etf_nav_history(wrong_instrument)

    wrong_index = _history_payload(include_correction=False)
    index_records = wrong_index["records"]
    assert isinstance(index_records, list)
    index_records[1]["underlying_index_id"] = "index_wrong_v1"
    with pytest.raises(EtfNavContractError, match="underlying_index_mismatch"):
        parse_etf_nav_history(wrong_index)

    duplicate = _history_payload(include_correction=False)
    duplicate_records = duplicate["records"]
    assert isinstance(duplicate_records, list)
    duplicate_records.append(copy.deepcopy(duplicate_records[1]))
    with pytest.raises(EtfNavContractError, match="nav_version_id_duplicate"):
        parse_etf_nav_history(duplicate)

    broken = _history_payload()
    broken_records = broken["records"]
    assert isinstance(broken_records, list)
    broken_records[2]["supersedes_version_id"] = "navv_unrelated_demo_0001_v1"
    with pytest.raises(EtfNavContractError, match="revision_chain_broken"):
        parse_etf_nav_history(broken)


def test_etf_nav_source_must_be_absolute_and_repository_external() -> None:
    relative = _history_payload(include_correction=False)
    relative["source_uri"] = "inputs/etf-nav.json"
    with pytest.raises(EtfNavContractError, match="absolute_local_artifact"):
        parse_etf_nav_history(relative)

    internal = _history_payload(include_correction=False)
    internal["source_uri"] = str(
        Path(__file__).resolve().parent / "fixtures" / "etf-nav.json"
    )
    with pytest.raises(EtfNavContractError, match="repository_external"):
        parse_etf_nav_history(internal)


def test_manifest_rejects_etf_nav_for_non_etf_and_wrong_index() -> None:
    non_etf = _manifest_with_domain_contracts()
    non_etf["etf_nav"] = _history_payload()
    with pytest.raises(ManifestValidationError, match="requires_etf_instrument"):
        parse_builtin_manifest(non_etf)

    wrong_index = _etf_manifest_payload()
    nav = wrong_index["etf_nav"]
    assert isinstance(nav, dict)
    nav["underlying_index_id"] = "index_wrong_v1"
    nav_records = nav["records"]
    assert isinstance(nav_records, list)
    for record in nav_records:
        record["underlying_index_id"] = "index_wrong_v1"
    with pytest.raises(ManifestValidationError, match="underlying_index_mismatch"):
        parse_builtin_manifest(wrong_index)


def test_manifest_dataset_and_package_bind_etf_nav_contract(tmp_path: Path) -> None:
    manifest = parse_builtin_manifest(_etf_manifest_payload())
    canonical = manifest.canonical_payload()
    seed_scope = manifest.simulation_seed_scope_payload()
    evidence = manifest.instrument_evidence()

    assert canonical["etf_nav"] == manifest.etf_nav.as_dict()  # type: ignore[union-attr]
    assert "etf_nav" in seed_scope
    nav_evidence = evidence["etf_nav"]
    assert isinstance(nav_evidence, dict)
    assert nav_evidence["etf_nav_contract_hash"] == manifest.etf_nav.contract_hash()  # type: ignore[union-attr]

    db_path = tmp_path / "candles.sqlite"
    _write_candles(db_path)
    snapshot = load_dataset_split(
        db_path=db_path, manifest=manifest, split_name="train"
    )
    without_nav = parse_builtin_manifest(_etf_manifest_payload(include_nav=False))
    snapshot_without_nav = load_dataset_split(
        db_path=db_path, manifest=without_nav, split_name="train"
    )
    domain = (snapshot.options or {})["domain_contracts"]
    assert domain["etf_nav"]["etf_nav_contract_hash"] == (
        manifest.etf_nav.contract_hash()  # type: ignore[union-attr]
    )
    assert snapshot.snapshot_query_hash() != snapshot_without_nav.snapshot_query_hash()

    projected = _project_target_asset(
        {
            "target_asset": {
                "market": manifest.market,
                "interval": manifest.interval,
                "instrument_evidence": evidence,
            }
        }
    )
    projected_evidence = projected["instrument_evidence"]
    assert isinstance(projected_evidence, dict)
    projected_nav = projected_evidence["etf_nav"]
    assert isinstance(projected_nav, dict)
    assert "source_uri" not in projected_nav
    assert projected_nav["source_manifest_hash"] == _hash("5")
    assert projected_nav["etf_nav_contract_hash"] == manifest.etf_nav.contract_hash()  # type: ignore[union-attr]


def test_production_pit_materialization_resolves_only_then_known_nav(
    tmp_path: Path,
) -> None:
    payload = _etf_manifest_payload()
    payload["universe"] = _universe_payload()
    payload["market_calendar"] = _session_calendar_payload()
    manifest = parse_builtin_manifest(payload)
    db_path = tmp_path / "pit-candles.sqlite"
    _write_candles(db_path, ts=1767286800000)  # 2026-01-01T17:00:00Z

    snapshot = load_dataset_split(
        db_path=db_path, manifest=manifest, split_name="train"
    )
    pit = snapshot.point_in_time_decision_evidence
    assert isinstance(pit, dict)
    authorities = pit["authorities"]
    assert isinstance(authorities, dict)
    assert authorities["etf_nav"]["etf_nav_contract_hash"] == (
        manifest.etf_nav.contract_hash()  # type: ignore[union-attr]
    )
    rows = pit["rows"]
    assert isinstance(rows, (list, tuple))
    selected_nav = rows[0]["latest_known_etf_nav"]
    assert selected_nav["official_nav"]["revision"] == 1
    assert selected_nav["inav"]["revision"] == 1
