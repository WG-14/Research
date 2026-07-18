from __future__ import annotations

import copy
import sqlite3
from decimal import Decimal

import pytest

from market_research.research.corporate_action_contract import (
    CorporateActionContractError,
    parse_corporate_action_set,
)
from market_research.research.dataset_snapshot import load_dataset_split
from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.instrument_contract import (
    GenericPositionLeg,
    InstrumentContractError,
    Money,
    Ratio,
    parse_instrument_master,
)
from market_research.research_composition import parse_builtin_manifest
from tests.test_research_semantics_v2_contract import _manifest_payload


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _instrument(*, asset_type: str = "spot") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "instrument_id": "inst_btc_internal_0001",
        "instrument_version_id": "instv_btc_internal_0001_v1",
        "version": 1,
        "asset_type": asset_type,
        "exchange_mic": "XOFF",
        "trading_currency": "KRW",
        "price_tick": "0.01",
        "quantity_step": "0.0001",
        "trading_unit": "1",
        "listed_on": "2017-01-01",
        "delisted_on": None,
        "name_history": [
            {
                "name": "Bitcoin research instrument",
                "effective_from": "2017-01-01T00:00:00+00:00",
                "effective_to": None,
            }
        ],
        "vendor_mappings": [
            {
                "provider_id": "manifest_market",
                "symbol": "KRW-BTC",
                "effective_from": "2017-01-01T00:00:00+00:00",
                "effective_to": None,
            },
            {
                "provider_id": "prepared_vendor_a",
                "symbol": "XBTKRW",
                "effective_from": "2017-01-01T00:00:00+00:00",
                "effective_to": None,
            },
        ],
        "etf_underlying_index_id": None,
        "futures": None,
        "option": None,
        "source": "manifest",
    }
    return payload


def _events() -> dict[str, object]:
    return {
        "schema_version": 1,
        "instrument_id": "inst_btc_internal_0001",
        "action_set_id": "cas_btc_actions_0001",
        "events": [
            {
                "schema_version": 1,
                "event_id": "ca_btc_split_0001",
                "event_version_id": "cav_btc_split_0001_v1",
                "version": 1,
                "instrument_id": "inst_btc_internal_0001",
                "event_type": "split",
                "effective_at": "2026-01-02T00:00:00+00:00",
                "published_at": "2026-01-03T00:00:00+00:00",
                "observed_at": "2026-01-04T00:00:00+00:00",
                "source_content_hash": _hash("a"),
                "ratio": "2",
                "cash_amount": None,
                "cash_currency": None,
                "replacement_symbol": None,
                "replacement_instrument_id": None,
                "tradability": None,
            }
        ],
    }


def _manifest_with_domain_contracts() -> dict[str, object]:
    payload = copy.deepcopy(_manifest_payload())
    payload["instrument"] = _instrument()
    payload["corporate_action_set"] = _events()
    action_set = parse_corporate_action_set(
        payload["corporate_action_set"],
        expected_instrument_id="inst_btc_internal_0001",
    )
    payload["corporate_action_policy"] = {
        "schema_version": 1,
        "policy_id": "cap_raw_prices_v1",
        "version": 1,
        "price_series": "raw",
        "price_adjustment": "none",
        "volume_adjustment": "none",
        "dividend_treatment": "cash_flow_separate",
        "action_set_hash": action_set.contract_hash(),
    }
    return payload


def test_manifest_separates_internal_identity_from_vendor_symbols_and_hashes_units():
    manifest = parse_builtin_manifest(_manifest_with_domain_contracts())

    assert manifest.market == "KRW-BTC"
    assert manifest.instrument.instrument_id == "inst_btc_internal_0001"
    assert manifest.instrument.instrument_version_id == "instv_btc_internal_0001_v1"
    assert manifest.instrument.price_tick == Decimal("0.01")
    assert manifest.instrument.quantity_step == Decimal("0.0001")
    assert {item.symbol for item in manifest.instrument.vendor_mappings} == {
        "KRW-BTC",
        "XBTKRW",
    }
    evidence = manifest.instrument_evidence()
    assert evidence["instrument_contract_hash"].startswith("sha256:")
    assert evidence["corporate_action_set_hash"] == (
        manifest.corporate_action_set.contract_hash()
    )
    assert manifest.canonical_payload()["instrument"] == manifest.instrument.as_dict()


def test_production_dataset_materialization_binds_domain_contract_hashes(tmp_path):
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

    manifest = parse_builtin_manifest(_manifest_with_domain_contracts())
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )

    domain = (snapshot.options or {})["domain_contracts"]
    assert domain["instrument"]["instrument_id"] == "inst_btc_internal_0001"
    assert domain["corporate_actions"]["action_set_hash"] == (
        manifest.corporate_action_set.contract_hash()
    )
    assert snapshot.snapshot_query_hash().startswith("sha256:")


def test_decimal_unit_contract_rejects_float_and_off_tick_values():
    instrument = parse_instrument_master(_instrument())
    assert instrument.validate_price("100.25") == Decimal("100.25")
    assert instrument.validate_quantity("1.2345") == Decimal("1.2345")
    assert instrument.round_quantity("1.23456", policy="down") == Decimal("1.2345")
    with pytest.raises(InstrumentContractError, match="price_not_aligned"):
        instrument.validate_price("100.255")
    with pytest.raises(InstrumentContractError, match="must_be_decimal_string"):
        instrument.validate_price(100.25)
    assert Ratio(Decimal("0.02")).as_dict() == {
        "value": "0.02",
        "unit": "ratio_1_equals_100_percent",
    }


def test_vendor_mapping_ranges_and_manifest_market_mapping_fail_closed():
    overlapping = _instrument()
    mappings = overlapping["vendor_mappings"]
    assert isinstance(mappings, list)
    mappings.append(
        {
            "provider_id": "prepared_vendor_a",
            "symbol": "BTC-KRW",
            "effective_from": "2020-01-01T00:00:00+00:00",
            "effective_to": None,
        }
    )
    with pytest.raises(InstrumentContractError, match="ranges_overlap"):
        parse_instrument_master(overlapping)

    missing = _manifest_with_domain_contracts()
    instrument_payload = missing["instrument"]
    assert isinstance(instrument_payload, dict)
    instrument_payload["vendor_mappings"] = [
        item
        for item in instrument_payload["vendor_mappings"]
        if item["provider_id"] != "manifest_market"
    ]
    with pytest.raises(ManifestValidationError, match="market_mapping_missing"):
        parse_builtin_manifest(missing)


def test_corporate_action_uses_knowledge_time_not_effective_time():
    action_set = parse_corporate_action_set(
        _events(), expected_instrument_id="inst_btc_internal_0001"
    )
    event = action_set.events[0]
    assert event.is_effective_at("2026-01-02T00:00:00+00:00")
    assert not event.is_known_at("2026-01-03T23:59:59+00:00")
    assert action_set.effective_and_known(as_of="2026-01-03T23:59:59+00:00") == ()
    assert action_set.effective_and_known(as_of="2026-01-04T00:00:00+00:00") == (event,)

    invalid = _events()
    invalid_event = invalid["events"][0]
    invalid_event["observed_at"] = "2026-01-02T00:00:00+00:00"
    with pytest.raises(
        CorporateActionContractError, match="observed_before_publication"
    ):
        parse_corporate_action_set(
            invalid, expected_instrument_id="inst_btc_internal_0001"
        )


def test_derivative_extensions_are_typed_but_active_engine_use_is_rejected():
    future = _instrument(asset_type="future")
    future["futures"] = {
        "contract_code": "BTC-202612",
        "underlying_instrument_id": "inst_btc_spot_base_0001",
        "expiry_at": "2026-12-18T08:00:00+00:00",
        "contract_multiplier": "1",
        "margin_currency": "KRW",
        "initial_margin_ratio": "0.10",
        "maintenance_margin_ratio": "0.08",
        "settlement_type": "cash",
        "continuous_series_policy_id": "front_month_back_adjusted_v1",
        "roll_policy_id": "volume_crossover_v1",
        "basis_unit": "quote_currency_per_contract",
        "session_calendar_id": "calendar_xoff_night_v1",
        "max_leverage_ratio": "5",
    }
    parsed_future = parse_instrument_master(future)
    assert parsed_future.futures is not None
    assert parsed_future.futures.contract_multiplier == Decimal("1")

    option = _instrument(asset_type="option")
    option["option"] = {
        "option_type": "call",
        "underlying_instrument_id": "inst_btc_spot_base_0001",
        "strike_price": "100000000",
        "expiry_at": "2026-12-18T08:00:00+00:00",
        "contract_multiplier": "0.01",
        "premium_currency": "KRW",
        "settlement_type": "cash",
        "greeks_policy_id": "black_scholes_greeks_v1",
        "implied_volatility_policy_id": "mid_quote_iv_v1",
        "volatility_surface_id": "surface_btc_202612_v1",
        "position_group_policy_id": "multi_leg_net_greeks_v1",
        "expiry_payoff_policy_id": "cash_intrinsic_v1",
        "liquidity_policy_id": "option_spread_oi_v1",
    }
    parsed_option = parse_instrument_master(option)
    assert parsed_option.option is not None
    assert parsed_option.option.option_type == "call"

    leg = GenericPositionLeg(
        instrument_id="inst_btc_internal_0001",
        quantity=Decimal("2"),
        quantity_unit="contracts",
        entry_price=Money(Decimal("10.25"), "KRW"),
        contract_multiplier=Decimal("0.01"),
        side="long",
        leg_id="call_leg_1",
    )
    assert leg.as_dict()["quantity_unit"] == "contracts"

    manifest = _manifest_with_domain_contracts()
    manifest["instrument"] = future
    with pytest.raises(ManifestValidationError, match="not_supported.*future"):
        parse_builtin_manifest(manifest)


def test_explicit_instrument_requires_versioned_action_policy_and_currency_match():
    missing = copy.deepcopy(_manifest_payload())
    missing["instrument"] = _instrument()
    with pytest.raises(ManifestValidationError, match="requires_corporate_action"):
        parse_builtin_manifest(missing)

    mismatch = _manifest_with_domain_contracts()
    instrument = mismatch["instrument"]
    assert isinstance(instrument, dict)
    instrument["trading_currency"] = "USD"
    with pytest.raises(ManifestValidationError, match="quote_currency_must_match"):
        parse_builtin_manifest(mismatch)

    disguised_legacy = _manifest_with_domain_contracts()
    disguised_instrument = disguised_legacy["instrument"]
    assert isinstance(disguised_instrument, dict)
    disguised_instrument["source"] = "legacy_market_mapping"
    with pytest.raises(ManifestValidationError, match="source_must_be_manifest"):
        parse_builtin_manifest(disguised_legacy)
