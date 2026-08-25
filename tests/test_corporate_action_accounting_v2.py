from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.research.corporate_action_contract import (
    CorporateActionContractError,
    corporate_action_embedded_event_material_hash,
    parse_corporate_action_set,
)
from market_research.research.dataset_snapshot import load_dataset_split
from market_research.research.corporate_action_portfolio import (
    parse_corporate_action_portfolio_plan,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.portfolio_ledger import PortfolioLedger
from market_research.research_composition import parse_builtin_manifest
from tests.test_corporate_action_dataset_materialization import (
    _run_official_buy_and_hold,
    _write_candles,
)
from tests.test_instrument_domain_contracts import _manifest_with_domain_contracts


ROOT_INSTRUMENT = "inst_btc_internal_0001"
REPLACEMENT_TWO = "inst_btc_replacement_0002"
REPLACEMENT_THREE = "inst_btc_replacement_0003"


def _hash(char: str = "a") -> str:
    return "sha256:" + char * 64


def _terms(
    *,
    settlement_policy: str,
    sequence: int = 1,
    position_effect: str = "unchanged",
    position_ratio: str = "1",
    cash_per_unit: str = "0",
    cash_currency: str | None = None,
    tax_policy: str = "none",
    tax_rate: str = "0",
    basis_policy: str = "preserve",
    cash_basis_fraction: str = "0",
    fractional_policy: str = "retain_exact",
    cash_in_lieu_price: str | None = None,
    cash_in_lieu_tax_rate: str = "0",
    terminal: bool = False,
    continuation_price_policy: str = "not_applicable",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "settlement_policy": settlement_policy,
        "same_timestamp_sequence": sequence,
        "entitlement_basis": "position_immediately_before_event",
        "position_effect": position_effect,
        "position_ratio": position_ratio,
        "cash_per_pre_event_unit": cash_per_unit,
        "cash_currency": cash_currency,
        "tax_policy": tax_policy,
        "tax_rate": tax_rate,
        "basis_policy": basis_policy,
        "cash_basis_fraction": cash_basis_fraction,
        "fractional_policy": fractional_policy,
        "cash_in_lieu_price": cash_in_lieu_price,
        "cash_in_lieu_tax_rate": cash_in_lieu_tax_rate,
        "terminal": terminal,
        "continuation_price_policy": continuation_price_policy,
    }


def _event(
    *,
    event_id: str,
    event_type: str,
    effective_at: str,
    terms: dict[str, object],
    instrument_id: str = ROOT_INSTRUMENT,
    replacement_instrument_id: str | None = None,
    tradability: str | None = None,
    observed_at: str | None = None,
) -> dict[str, object]:
    resolved_observed_at = observed_at or effective_at
    position_effect = terms["position_effect"]
    cash_policy = terms["settlement_policy"] in {
        "cash_distribution",
        "capital_reduction",
        "rights_cash_entitlement",
        "rights_subscription",
        "cash_settled_spin_off",
        "mixed_merger",
        "cash_exit",
    }
    payload: dict[str, object] = {
        "schema_version": 2,
        "event_id": event_id,
        "event_version_id": f"cav_{event_id[3:]}_v1",
        "version": 1,
        "instrument_id": instrument_id,
        "event_type": event_type,
        "effective_at": effective_at,
        "published_at": resolved_observed_at,
        "observed_at": resolved_observed_at,
        "source_content_hash": _hash(event_id[-1]),
        "embedded_event_material_hash": "",
        "ratio": (
            terms["position_ratio"] if position_effect in {"scale", "replace"} else None
        ),
        "cash_amount": terms["cash_per_pre_event_unit"] if cash_policy else None,
        "cash_currency": terms["cash_currency"],
        "replacement_symbol": None,
        "replacement_instrument_id": replacement_instrument_id,
        "tradability": tradability,
        "accounting_terms": terms,
    }
    payload["embedded_event_material_hash"] = (
        corporate_action_embedded_event_material_hash(payload)
    )
    return payload


def _manifest(events: list[dict[str, object]]):
    payload = copy.deepcopy(_manifest_with_domain_contracts())
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset["train"] = {"start": "2026-01-01", "end": "2026-01-05"}
    dataset["validation"] = {"start": "2026-01-10", "end": "2026-01-10"}
    options = dataset.setdefault("options", {})
    assert isinstance(options, dict)
    options["corporate_action_known_at"] = "2026-01-07T00:00:00+00:00"
    canonical_events = sorted(
        events,
        key=lambda item: (
            str(item["effective_at"]),
            str(item["observed_at"]),
            str(item["event_id"]),
            int(item["version"]),
        ),
    )
    action_set_payload = {
        "schema_version": 2,
        "instrument_id": ROOT_INSTRUMENT,
        "action_set_id": "cas_btc_accounting_v2_0001",
        "events": canonical_events,
    }
    action_set = parse_corporate_action_set(
        action_set_payload,
        expected_instrument_id=ROOT_INSTRUMENT,
    )
    payload["corporate_action_set"] = action_set_payload
    payload["corporate_action_policy"] = {
        "schema_version": 2,
        "policy_id": "cap_causal_accounting_v2_0001",
        "version": 2,
        "price_series": "raw",
        "price_adjustment": "none",
        "volume_adjustment": "none",
        "dividend_treatment": "cash_flow_separate",
        "action_set_hash": action_set.contract_hash(),
    }
    return parse_builtin_manifest(payload)


def _run(tmp_path: Path, events: list[dict[str, object]]):
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    manifest = _manifest(events)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    return (
        manifest,
        snapshot,
        _run_official_buy_and_hold(
            manifest=manifest,
            snapshot=snapshot,
        ),
    )


def test_mixed_and_stock_merger_fail_without_replacement_price_series_binding(
    tmp_path: Path,
) -> None:
    mixed = _event(
        event_id="ca_btc_mixed_merger_0001",
        event_type="merger",
        effective_at="2026-01-03T00:00:00+00:00",
        replacement_instrument_id=REPLACEMENT_TWO,
        terms=_terms(
            settlement_policy="mixed_merger",
            position_effect="replace",
            position_ratio="0.5",
            cash_per_unit="55",
            cash_currency="KRW",
            tax_policy="gain_over_allocated_basis_rate",
            tax_rate="0.1",
            basis_policy="allocate_cash_fraction",
            cash_basis_fraction="0.4",
            continuation_price_policy=(
                "prepared_raw_series_switches_to_replacement_at_effective"
            ),
        ),
    )
    stock = _event(
        event_id="ca_btc_stock_merger_0002",
        event_type="merger",
        effective_at="2026-01-04T00:00:00+00:00",
        instrument_id=REPLACEMENT_TWO,
        replacement_instrument_id=REPLACEMENT_THREE,
        terms=_terms(
            settlement_policy="stock_merger",
            position_effect="replace",
            position_ratio="2",
            basis_policy="allocate_cash_fraction",
            continuation_price_policy=(
                "prepared_raw_series_switches_to_replacement_at_effective"
            ),
        ),
    )

    path = tmp_path / "candles.sqlite"
    _write_candles(path)

    with pytest.raises(
        CorporateActionContractError,
        match="replacement_price_series_binding_required",
    ):
        load_dataset_split(
            db_path=path,
            manifest=_manifest([mixed, stock]),
            split_name="train",
        )


@pytest.mark.parametrize(
    ("split_suffix", "dividend_suffix"),
    [("aaa", "zzz"), ("zzz", "aaa")],
)
def test_same_timestamp_explicit_sequence_controls_entitlement_not_event_id(
    tmp_path: Path,
    split_suffix: str,
    dividend_suffix: str,
) -> None:
    split = _event(
        event_id=f"ca_btc_order_split_{split_suffix}_0001",
        event_type="split",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="quantity_adjustment",
            sequence=1,
            position_effect="scale",
            position_ratio="2",
        ),
    )
    dividend = _event(
        event_id=f"ca_btc_order_dividend_{dividend_suffix}_0001",
        event_type="cash_dividend",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="cash_distribution",
            sequence=2,
            cash_per_unit="5",
            cash_currency="KRW",
        ),
    )

    _manifest_value, snapshot, run = _run(tmp_path, [dividend, split])
    entries = [
        item for item in run.ledger_entries if item.entry_type == "corporate_action"
    ]

    assert [
        item.corporate_action_event["event"]["event_type"]
        for item in entries
        if item.corporate_action_event is not None
    ] == ["split", "cash_dividend"]
    assert entries[1].asset_qty_before == pytest.approx(18_000)
    assert entries[1].cash_delta == pytest.approx(90_000)
    materialization = snapshot.corporate_action_transformation_evidence
    assert materialization is not None
    plan = materialization["portfolio_event_plan"]
    assert plan["same_timestamp_action_policy"] == (
        "explicit_unique_sequence_ascending_then_event_id_else_fail_closed"
    )


def test_same_timestamp_duplicate_sequence_fails_closed_before_simulation(
    tmp_path: Path,
) -> None:
    events = [
        _event(
            event_id="ca_btc_duplicate_split_0001",
            event_type="split",
            effective_at="2026-01-03T00:00:00+00:00",
            terms=_terms(
                settlement_policy="quantity_adjustment",
                position_effect="scale",
                position_ratio="2",
            ),
        ),
        _event(
            event_id="ca_btc_duplicate_cash_0002",
            event_type="cash_dividend",
            effective_at="2026-01-03T00:00:00+00:00",
            terms=_terms(
                settlement_policy="cash_distribution",
                cash_per_unit="5",
                cash_currency="KRW",
            ),
        ),
    ]
    path = tmp_path / "candles.sqlite"
    _write_candles(path)

    with pytest.raises(
        CorporateActionContractError,
        match="same_timestamp_event_ordering_terms_required",
    ):
        load_dataset_split(
            db_path=path,
            manifest=_manifest(events),
            split_name="train",
        )


def test_rights_subscription_and_ex_rights_are_explicit_ordered_accounting(
    tmp_path: Path,
) -> None:
    ex_rights = _event(
        event_id="ca_btc_ex_rights_marker_0001",
        event_type="ex_rights",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(settlement_policy="ex_rights_marker", sequence=1),
    )
    subscription = _event(
        event_id="ca_btc_rights_subscription_0002",
        event_type="rights_issue",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="rights_subscription",
            sequence=2,
            position_effect="scale",
            position_ratio="1.25",
            cash_per_unit="-1",
            cash_currency="KRW",
            basis_policy="add_cash_outflow",
        ),
    )

    _manifest_value, _snapshot, run = _run(tmp_path, [subscription, ex_rights])
    entries = [
        item for item in run.ledger_entries if item.entry_type == "corporate_action"
    ]

    assert [
        item.corporate_action_event["event"]["event_type"]
        for item in entries
        if item.corporate_action_event is not None
    ] == ["ex_rights", "rights_issue"]
    assert entries[-1].asset_qty_after == pytest.approx(11_250)
    assert entries[-1].cash_delta == pytest.approx(-9_000)
    assert entries[-1].cost_basis_after == pytest.approx(999_000)
    assert entries[-1].realized_pnl == pytest.approx(0)


@pytest.mark.parametrize(
    ("event", "expected_qty", "expected_cash_delta", "expected_basis", "expected_pnl"),
    [
        (
            _event(
                event_id="ca_btc_capital_reduction_0001",
                event_type="capital_reduction",
                effective_at="2026-01-03T00:00:00+00:00",
                terms=_terms(
                    settlement_policy="capital_reduction",
                    position_effect="scale",
                    position_ratio="0.5",
                    cash_per_unit="10",
                    cash_currency="KRW",
                    tax_policy="gain_over_allocated_basis_rate",
                    tax_rate="0.2",
                    basis_policy="allocate_cash_fraction",
                    cash_basis_fraction="0.1",
                ),
            ),
            4_500,
            90_000,
            891_000,
            -9_000,
        ),
        (
            _event(
                event_id="ca_btc_cash_spin_off_0002",
                event_type="spin_off",
                effective_at="2026-01-03T00:00:00+00:00",
                replacement_instrument_id=REPLACEMENT_TWO,
                terms=_terms(
                    settlement_policy="cash_settled_spin_off",
                    cash_per_unit="10",
                    cash_currency="KRW",
                    tax_policy="gain_over_allocated_basis_rate",
                    tax_rate="0.2",
                    basis_policy="allocate_cash_fraction",
                    cash_basis_fraction="0.05",
                ),
            ),
            9_000,
            81_900,
            940_500,
            32_400,
        ),
    ],
)
def test_capital_reduction_and_cash_settled_spin_off_accounting_benchmarks(
    tmp_path: Path,
    event: dict[str, object],
    expected_qty: float,
    expected_cash_delta: float,
    expected_basis: float,
    expected_pnl: float,
) -> None:
    _manifest_value, _snapshot, run = _run(tmp_path, [event])
    entry = next(
        item for item in run.ledger_entries if item.entry_type == "corporate_action"
    )

    assert entry.asset_qty_after == pytest.approx(expected_qty)
    assert entry.cash_delta == pytest.approx(expected_cash_delta)
    assert entry.cost_basis_after == pytest.approx(expected_basis)
    assert entry.realized_pnl == pytest.approx(expected_pnl)
    assert entry.corporate_action_accounting is not None
    assert entry.corporate_action_accounting["accounting_identity_status"] == "PASS"


def test_fractional_quantity_cash_in_lieu_is_taxed_and_hash_bound(
    tmp_path: Path,
) -> None:
    fractional = _event(
        event_id="ca_btc_fractional_split_0001",
        event_type="reverse_split",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="quantity_adjustment",
            position_effect="scale",
            position_ratio="0.33333333",
            cash_currency="KRW",
            fractional_policy="round_down_cash_in_lieu",
            cash_in_lieu_price="1000",
            cash_in_lieu_tax_rate="0.2",
        ),
    )

    _manifest_value, _snapshot, run = _run(tmp_path, [fractional])
    entry = next(
        item for item in run.ledger_entries if item.entry_type == "corporate_action"
    )
    accounting = entry.corporate_action_accounting
    assert accounting is not None

    assert entry.asset_qty_after == pytest.approx(2_999.9999)
    assert float(accounting["fractional_quantity"]) > 0
    assert float(accounting["cash_in_lieu_gross"]) > 0
    assert float(accounting["cash_in_lieu_tax"]) > 0
    assert str(accounting["accounting_hash"]).startswith("sha256:")


def test_explicit_terminal_cash_exit_closes_identity_and_basis(tmp_path: Path) -> None:
    terminal = _event(
        event_id="ca_btc_explicit_delisting_0001",
        event_type="delisting",
        effective_at="2026-01-05T00:01:00+00:00",
        tradability="delisted",
        terms=_terms(
            settlement_policy="cash_exit",
            position_effect="close",
            position_ratio="0",
            cash_per_unit="55",
            cash_currency="KRW",
            tax_policy="gain_over_allocated_basis_rate",
            tax_rate="0.2",
            basis_policy="close",
            cash_basis_fraction="1",
            terminal=True,
        ),
    )

    _manifest_value, _snapshot, run = _run(tmp_path, [terminal])

    assert run.resource_usage["final_asset_qty"] == pytest.approx(0)
    assert run.resource_usage["final_asset_instrument_id"] is None
    assert run.resource_usage["final_cash"] == pytest.approx(505_000)
    assert run.closed_trades[0].exit_reason == "delisting"


def test_schema_v2_missing_or_ambiguous_terms_fail_closed() -> None:
    valid = _event(
        event_id="ca_btc_missing_terms_0001",
        event_type="cash_dividend",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="cash_distribution",
            cash_per_unit="5",
            cash_currency="KRW",
        ),
    )
    no_terms = dict(valid)
    no_terms.pop("accounting_terms")
    with pytest.raises(
        CorporateActionContractError,
        match="accounting_terms_required_for_schema_v2",
    ):
        _manifest([no_terms])

    missing_tax = copy.deepcopy(valid)
    terms = missing_tax["accounting_terms"]
    assert isinstance(terms, dict)
    terms.pop("tax_policy")
    with pytest.raises(CorporateActionContractError, match="missing_fields:tax_policy"):
        _manifest([missing_tax])

    ambiguous_ratio = copy.deepcopy(valid)
    ambiguous_ratio["ratio"] = "2"
    with pytest.raises(CorporateActionContractError, match="ratio_not_applicable"):
        _manifest([ambiguous_ratio])


def test_schema_v2_embedded_event_material_hash_rejects_tamper() -> None:
    event = _event(
        event_id="ca_btc_source_binding_0001",
        event_type="cash_dividend",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="cash_distribution",
            cash_per_unit="5",
            cash_currency="KRW",
        ),
    )
    event["embedded_event_material_hash"] = _hash("f")

    with pytest.raises(
        CorporateActionContractError,
        match="embedded_event_material_hash_content_mismatch",
    ):
        _manifest([event])


@pytest.mark.parametrize(
    "terms",
    [
        _terms(
            settlement_policy="cash_distribution",
            cash_per_unit="5",
            cash_currency="KRW",
            fractional_policy="round_down_cash_in_lieu",
            cash_in_lieu_price="100",
            cash_in_lieu_tax_rate="0.2",
        ),
        _terms(
            settlement_policy="quantity_adjustment",
            position_effect="scale",
            position_ratio="2",
            tax_policy="gross_cash_rate",
            tax_rate="0.2",
        ),
    ],
)
def test_schema_v2_rejects_semantically_inapplicable_accounting_terms(
    terms: dict[str, object],
) -> None:
    event_type = (
        "cash_dividend"
        if terms["settlement_policy"] == "cash_distribution"
        else "split"
    )
    event = _event(
        event_id=f"ca_btc_inapplicable_{event_type}_0001",
        event_type=event_type,
        effective_at="2026-01-03T00:00:00+00:00",
        terms=terms,
    )

    with pytest.raises(
        CorporateActionContractError,
        match=("fractional_policy_not_applicable|primary_tax_not_applicable"),
    ):
        _manifest([event])


def test_replay_rejects_late_observation_even_when_stream_is_self_rehashed(
    tmp_path: Path,
) -> None:
    event = _event(
        event_id="ca_btc_replay_late_0001",
        event_type="cash_dividend",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="cash_distribution",
            cash_per_unit="5",
            cash_currency="KRW",
        ),
    )
    manifest, _snapshot, run = _run(tmp_path, [event])
    entries = list(run.ledger_entries)
    index = next(
        index
        for index, entry in enumerate(entries)
        if entry.entry_type == "corporate_action"
    )
    wrapped = entries[index].corporate_action_event
    assert wrapped is not None
    payload = dict(wrapped["event"])
    payload["published_at"] = "2026-01-04T00:00:00+00:00"
    payload["observed_at"] = "2026-01-04T00:00:00+00:00"
    payload["embedded_event_material_hash"] = (
        corporate_action_embedded_event_material_hash(payload)
    )
    tampered_event = {
        "event": payload,
        "event_contract_hash": sha256_prefixed(
            payload,
            label="corporate_action_event",
        ),
    }
    entries[index] = replace(
        entries[index],
        corporate_action_event=tampered_event,
    )

    with pytest.raises(
        ValueError,
        match="late_observation_retroactive_accounting_unsupported",
    ):
        PortfolioLedger.replay(
            starting_cash=float(manifest.portfolio_policy.starting_cash_krw),
            initial_instrument_id=ROOT_INSTRUMENT,
            quantity_step=str(manifest.instrument.quantity_step),
            entries=entries,
        )


def test_replay_rejects_two_versions_of_one_event_identity(tmp_path: Path) -> None:
    first = _event(
        event_id="ca_btc_replay_correction_0001",
        event_type="cash_dividend",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="cash_distribution",
            cash_per_unit="5",
            cash_currency="KRW",
        ),
    )
    second = _event(
        event_id="ca_btc_replay_other_0002",
        event_type="cash_dividend",
        effective_at="2026-01-04T00:00:00+00:00",
        terms=_terms(
            settlement_policy="cash_distribution",
            cash_per_unit="7",
            cash_currency="KRW",
        ),
    )
    manifest, _snapshot, run = _run(tmp_path, [first, second])
    entries = list(run.ledger_entries)
    action_indexes = [
        index
        for index, entry in enumerate(entries)
        if entry.entry_type == "corporate_action"
    ]
    wrapped = entries[action_indexes[1]].corporate_action_event
    assert wrapped is not None
    payload = dict(wrapped["event"])
    payload["event_id"] = first["event_id"]
    payload["version"] = 2
    payload["embedded_event_material_hash"] = (
        corporate_action_embedded_event_material_hash(payload)
    )
    corrected_event = {
        "event": payload,
        "event_contract_hash": sha256_prefixed(
            payload,
            label="corporate_action_event",
        ),
    }
    entries[action_indexes[1]] = replace(
        entries[action_indexes[1]],
        corporate_action_event=corrected_event,
    )

    with pytest.raises(
        ValueError,
        match="correction_after_application_unsupported",
    ):
        PortfolioLedger.replay(
            starting_cash=float(manifest.portfolio_policy.starting_cash_krw),
            initial_instrument_id=ROOT_INSTRUMENT,
            quantity_step=str(manifest.instrument.quantity_step),
            entries=entries,
        )


def test_replay_plan_binding_rejects_missing_latest_event(tmp_path: Path) -> None:
    event = _event(
        event_id="ca_btc_replay_plan_0001",
        event_type="cash_dividend",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="cash_distribution",
            cash_per_unit="5",
            cash_currency="KRW",
        ),
    )
    manifest, snapshot, run = _run(tmp_path, [event])
    materialization = snapshot.corporate_action_transformation_evidence
    assert materialization is not None
    plan = parse_corporate_action_portfolio_plan(
        materialization["portfolio_event_plan"]
    )
    fill_entries = tuple(
        entry for entry in run.ledger_entries if entry.entry_type == "fill"
    )

    with pytest.raises(ValueError, match="event_set_mismatch"):
        PortfolioLedger.replay(
            starting_cash=float(manifest.portfolio_policy.starting_cash_krw),
            initial_instrument_id=ROOT_INSTRUMENT,
            quantity_step=str(manifest.instrument.quantity_step),
            corporate_action_plan=plan,
            causal_boundary_ms=plan.events[0].effective_ts_ms,
            entries=fill_entries,
        )


def test_schema_v2_future_known_event_and_backward_adjustment_fail_closed(
    tmp_path: Path,
) -> None:
    late = _event(
        event_id="ca_btc_future_known_split_0001",
        event_type="split",
        effective_at="2026-01-03T00:00:00+00:00",
        observed_at="2026-01-04T00:00:00+00:00",
        terms=_terms(
            settlement_policy="quantity_adjustment",
            position_effect="scale",
            position_ratio="2",
        ),
    )
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    with pytest.raises(
        CorporateActionContractError,
        match="late_initial_observation_retroactive_unsupported",
    ):
        load_dataset_split(
            db_path=path,
            manifest=_manifest([late]),
            split_name="train",
        )

    valid = _event(
        event_id="ca_btc_backward_split_0002",
        event_type="split",
        effective_at="2026-01-03T00:00:00+00:00",
        terms=_terms(
            settlement_policy="quantity_adjustment",
            position_effect="scale",
            position_ratio="2",
        ),
    )
    payload = _manifest([valid]).canonical_payload()
    policy = payload["corporate_action_policy"]
    assert isinstance(policy, dict)
    policy["price_series"] = "pre_adjusted"
    policy["price_adjustment"] = "backward_split_only"
    with pytest.raises(ValueError, match="schema_v2_requires_causal_raw_prices"):
        parse_builtin_manifest(payload)
