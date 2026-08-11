from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_research.research.causal_market_view import CausalMarketView
from market_research.research.corporate_action_contract import (
    CorporateActionContractError,
    parse_corporate_action_set,
)
from market_research.research.dataset_snapshot import (
    build_dataset_quality_report,
    load_dataset_split,
)
from market_research.research.datasets.hashing_contract import (
    snapshot_data_hash as calculate_snapshot_data_hash,
)
from market_research.research.experiment_manifest import (
    ExecutionScenario,
    ExperimentManifest,
)
from market_research.research.simulation_engine import (
    run_common_simulation_backtest,
)
from market_research.research.portfolio_ledger import PortfolioLedger
from market_research.research.validation_protocol import (
    _dataset_adapter_provenance_payload,
    _execution_model_from_scenario,
    _seed_context,
)
from market_research.research_composition import (
    parse_builtin_manifest,
    resolve_builtin_strategy,
)
from tests.test_instrument_domain_contracts import _manifest_with_domain_contracts
from tests.test_research_semantics_v2_contract import _manifest_payload


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _event(
    *,
    event_id: str,
    event_version_id: str,
    version: int,
    event_type: str,
    effective_at: str,
    observed_at: str,
    ratio: str | None = None,
    cash_amount: str | None = None,
    tradability: str | None = None,
    replacement_symbol: str | None = None,
    replacement_instrument_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_version_id": event_version_id,
        "version": version,
        "instrument_id": "inst_btc_internal_0001",
        "event_type": event_type,
        "effective_at": effective_at,
        "published_at": observed_at,
        "observed_at": observed_at,
        "source_content_hash": _hash(str(version)),
        "ratio": ratio,
        "cash_amount": cash_amount,
        "cash_currency": "KRW" if cash_amount is not None else None,
        "replacement_symbol": replacement_symbol,
        "replacement_instrument_id": replacement_instrument_id,
        "tradability": tradability,
    }


def _action_events(*, future_split_correction: bool = False) -> list[dict[str, object]]:
    events = [
        _event(
            event_id="ca_btc_loader_split_0001",
            event_version_id="cav_btc_loader_split_0001_v1",
            version=1,
            event_type="split",
            effective_at="2026-01-03T00:00:00+00:00",
            observed_at="2026-01-03T00:00:00+00:00",
            ratio="2",
        ),
        _event(
            event_id="ca_btc_loader_dividend_0001",
            event_version_id="cav_btc_loader_dividend_0001_v1",
            version=1,
            event_type="cash_dividend",
            effective_at="2026-01-04T00:00:00+00:00",
            observed_at="2026-01-04T00:00:00+00:00",
            cash_amount="5",
        ),
    ]
    if future_split_correction:
        events.append(
            _event(
                event_id="ca_btc_loader_split_0001",
                event_version_id="cav_btc_loader_split_0001_v2",
                version=2,
                event_type="split",
                effective_at="2026-01-03T00:00:00+00:00",
                observed_at="2026-01-06T00:00:00+00:00",
                ratio="4",
            )
        )
    return sorted(
        events,
        key=lambda item: (
            str(item["effective_at"]),
            str(item["observed_at"]),
            str(item["event_id"]),
            int(item["version"]),
        ),
    )


def _suffix_split_events(
    *,
    future_correction: bool = False,
    correction_effective_at: str = "2026-01-06T00:00:00+00:00",
) -> list[dict[str, object]]:
    events = [
        _event(
            event_id="ca_btc_loader_suffix_split_0001",
            event_version_id="cav_btc_loader_suffix_split_0001_v1",
            version=1,
            event_type="split",
            effective_at="2026-01-06T00:00:00+00:00",
            observed_at="2026-01-06T00:00:00+00:00",
            ratio="2",
        )
    ]
    if future_correction:
        events.append(
            _event(
                event_id="ca_btc_loader_suffix_split_0001",
                event_version_id="cav_btc_loader_suffix_split_0001_v2",
                version=2,
                event_type="split",
                effective_at=correction_effective_at,
                observed_at="2026-01-08T00:00:00+00:00",
                ratio="4",
            )
        )
    return sorted(
        events,
        key=lambda item: (
            str(item["effective_at"]),
            str(item["observed_at"]),
            str(item["event_id"]),
            int(item["version"]),
        ),
    )


def _manifest(
    *,
    events: list[dict[str, object]] | None = None,
    known_at: str | None = "2026-01-07T00:00:00+00:00",
    adjusted: bool = True,
    end: str = "2026-01-05",
) -> ExperimentManifest:
    payload = copy.deepcopy(_manifest_with_domain_contracts())
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset["train"] = {"start": "2026-01-01", "end": end}
    dataset["validation"] = {"start": "2026-01-10", "end": "2026-01-10"}
    options = dataset.setdefault("options", {})
    assert isinstance(options, dict)
    if known_at is None:
        options.pop("corporate_action_known_at", None)
    else:
        options["corporate_action_known_at"] = known_at
    action_set_payload = {
        "schema_version": 1,
        "instrument_id": "inst_btc_internal_0001",
        "action_set_id": "cas_btc_loader_actions_0001",
        "events": events if events is not None else _action_events(),
    }
    action_set = parse_corporate_action_set(
        action_set_payload,
        expected_instrument_id="inst_btc_internal_0001",
    )
    payload["corporate_action_set"] = action_set_payload
    payload["corporate_action_policy"] = {
        "schema_version": 1,
        "policy_id": (
            "cap_loader_adjusted_0001" if adjusted else "cap_loader_raw_0001"
        ),
        "version": 1,
        "price_series": "pre_adjusted" if adjusted else "raw",
        "price_adjustment": "backward_total_return" if adjusted else "none",
        "volume_adjustment": "inverse_split_factor" if adjusted else "none",
        "dividend_treatment": (
            "included_in_total_return_adjustment" if adjusted else "cash_flow_separate"
        ),
        "action_set_hash": action_set.contract_hash(),
    }
    return parse_builtin_manifest(payload)


def _epoch_ms(day: str) -> int:
    parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _write_candles(path: Path, *, through_day_six: bool = False) -> None:
    rows = [
        ("2026-01-01", 100.0, 101.0, 99.0, 100.0, 10.0),
        ("2026-01-02", 110.0, 112.0, 108.0, 110.0, 20.0),
        ("2026-01-03", 60.0, 61.0, 59.0, 60.0, 40.0),
        ("2026-01-04", 62.0, 63.0, 61.0, 62.0, 30.0),
        ("2026-01-05", 64.0, 65.0, 63.0, 64.0, 35.0),
    ]
    if through_day_six:
        rows.append(("2026-01-06", 1.0, 1.0, 1.0, 1.0, 0.0))
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE candles (pair TEXT NOT NULL, interval TEXT NOT NULL, "
            "ts INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
            "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("KRW-BTC", "1m", _epoch_ms(day), open_, high, low, close, volume)
                for day, open_, high, low, close, volume in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _run_official_buy_and_hold(
    *,
    manifest: ExperimentManifest,
    snapshot,
    buy_index: int = 0,
):
    return run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
        dataset=snapshot,
        parameter_values={"BUY_HOLD_BUY_INDEX": buy_index},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_timing_policy=manifest.execution_timing,
        portfolio_policy=manifest.portfolio_policy,
        risk_policy=manifest.risk_policy,
    )


def test_official_loader_hashes_out_of_period_action_authority_and_lineage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    manifest = _manifest(
        events=_suffix_split_events(),
        known_at="2026-01-07T00:00:00+00:00",
        adjusted=False,
    )
    assert (
        manifest.manifest_hash()
        != _manifest(
            events=_suffix_split_events(),
            known_at="2026-01-07T00:00:01+00:00",
            adjusted=False,
        ).manifest_hash()
    )
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )

    assert snapshot.candles[0].close == pytest.approx(100.0)
    assert snapshot.candles[0].volume == pytest.approx(10.0)
    evidence = snapshot.corporate_action_transformation_evidence
    assert evidence is not None
    assert evidence["applications"] == ()
    assert [
        item["event_version_id"] for item in evidence["selected_event_versions"]
    ] == ["cav_btc_loader_suffix_split_0001_v1"]
    assert evidence["known_at"] == "2026-01-05T00:01:00+00:00"
    assert evidence["manifest_known_at"] == "2026-01-07T00:00:00+00:00"
    assert evidence["known_at_authority"] == (
        "manifest.dataset.options.corporate_action_known_at"
    )
    assert evidence["strategy_snapshot_price_series_policy"] == (
        "raw_only_static_snapshot"
    )
    with pytest.raises(TypeError, match="immutable_contract_mutation_rejected"):
        evidence["known_at"] = "2026-01-06T00:00:00+00:00"
    assert evidence["input_rows_hash"] == evidence["output_rows_hash"]
    without_evidence = replace(
        snapshot,
        corporate_action_transformation_evidence=None,
    )
    assert snapshot.snapshot_data_hash() != without_evidence.snapshot_data_hash()
    quality = build_dataset_quality_report(db_path=path, snapshot=snapshot)
    assert (
        quality.payload["corporate_action_transformation_evidence_content_hash"]
        == evidence["content_hash"]
    )
    assert (
        quality.payload["corporate_action_known_at_authority_binding_hash"]
        == evidence["known_at_authority_binding_hash"]
    )
    assert quality.payload["corporate_action_portfolio_event_plan_hash"] == evidence[
        "portfolio_event_plan_hash"
    ]
    assert quality.payload["corporate_action_materialization_evidence_hash"] == evidence[
        "materialization_evidence_hash"
    ]
    lineage = _dataset_adapter_provenance_payload(
        manifest=manifest,
        snapshots=(snapshot,),
        quality_reports=(quality,),
    )
    assert lineage["corporate_action_transformation_evidence_hashes"] == {
        "train": evidence["content_hash"]
    }
    assert lineage["corporate_action_known_at_authority_binding_hashes"] == {
        "train": evidence["known_at_authority_binding_hash"]
    }
    assert lineage["corporate_action_portfolio_event_plan_hashes"] == {
        "train": evidence["portfolio_event_plan_hash"]
    }
    assert lineage["corporate_action_materialization_evidence_hashes"] == {
        "train": evidence["materialization_evidence_hash"]
    }


def test_official_loader_does_not_apply_future_corporate_action_correction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    original = load_dataset_split(
        db_path=path,
        manifest=_manifest(
            events=_suffix_split_events(),
            known_at="2026-01-07T00:00:00+00:00",
            adjusted=False,
        ),
        split_name="train",
    )
    with_future_correction = load_dataset_split(
        db_path=path,
        manifest=_manifest(
            events=_suffix_split_events(
                future_correction=True,
                correction_effective_at="2026-01-03T00:00:00+00:00",
            ),
            known_at="2026-01-07T00:00:00+00:00",
            adjusted=False,
        ),
        split_name="train",
    )

    assert with_future_correction.candles == original.candles
    evidence = with_future_correction.corporate_action_transformation_evidence
    assert evidence is not None
    split = next(
        item
        for item in evidence["selected_event_versions"]
        if item["event_type"] == "split"
    )
    assert split["version"] == 1
    assert split["event_version_id"] == "cav_btc_loader_suffix_split_0001_v1"
    assert with_future_correction.snapshot_data_hash() != original.snapshot_data_hash()


def test_official_strategy_loader_rejects_static_backward_adjustment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)

    with pytest.raises(
        CorporateActionContractError,
        match="static_backward_adjustment_not_causal_for_strategy",
    ):
        load_dataset_split(
            db_path=path,
            manifest=_manifest(),
            split_name="train",
        )


def test_raw_in_period_split_and_dividend_use_event_aware_portfolio_ledger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    manifest = _manifest(adjusted=False)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    run = _run_official_buy_and_hold(manifest=manifest, snapshot=snapshot)

    assert [candle.close for candle in snapshot.candles] == [
        100.0,
        110.0,
        60.0,
        62.0,
        64.0,
    ]
    action_entries = [
        entry for entry in run.ledger_entries if entry.entry_type == "corporate_action"
    ]
    assert [
        entry.corporate_action_event["event"]["event_type"]
        for entry in action_entries
        if entry.corporate_action_event is not None
    ] == ["split", "cash_dividend"]
    assert action_entries[0].asset_qty_before == pytest.approx(9_000)
    assert action_entries[0].asset_qty_after == pytest.approx(18_000)
    assert action_entries[0].cost_basis_after == pytest.approx(990_000)
    assert action_entries[0].cash_delta == pytest.approx(0)
    assert action_entries[1].asset_qty_after == pytest.approx(18_000)
    assert action_entries[1].cash_delta == pytest.approx(90_000)
    assert action_entries[1].realized_pnl == pytest.approx(90_000)
    evidence = run.execution_event_summary["corporate_action_portfolio_evidence"]
    source = snapshot.corporate_action_transformation_evidence
    assert source is not None
    assert evidence["portfolio_event_plan_hash"] == source["portfolio_event_plan_hash"]
    assert evidence["source_materialization_evidence_hash"] == source[
        "materialization_evidence_hash"
    ]
    assert evidence["accounting_replay_invariant_status"] == "PASS"
    replayed = PortfolioLedger.replay(
        starting_cash=float(manifest.portfolio_policy.starting_cash_krw),
        entries=run.ledger_entries,
    )
    assert replayed.cash == pytest.approx(run.resource_usage["final_cash"])
    tampered = tuple(
        replace(entry, cash_after=entry.cash_after + 1)
        if entry.ledger_entry_id == action_entries[-1].ledger_entry_id
        else entry
        for entry in run.ledger_entries
    )
    with pytest.raises(ValueError, match="corporate_action_transition_mismatch"):
        PortfolioLedger.replay(
            starting_cash=float(manifest.portfolio_policy.starting_cash_krw),
            entries=tampered,
        )


def test_pre_period_trading_halt_blocks_official_strategy_execution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    halt = _event(
        event_id="ca_btc_loader_halt_0001",
        event_version_id="cav_btc_loader_halt_0001_v1",
        version=1,
        event_type="trading_halt",
        effective_at="2025-12-31T00:00:00+00:00",
        observed_at="2025-12-31T00:00:00+00:00",
        tradability="halted",
    )

    manifest = _manifest(
        events=[halt],
        known_at="2026-01-07T00:00:00+00:00",
        adjusted=False,
    )
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    run = _run_official_buy_and_hold(manifest=manifest, snapshot=snapshot)

    assert run.fills == ()
    assert run.order_intents == ()
    assert len(run.ledger_entries) == 1
    evidence = run.execution_event_summary["corporate_action_portfolio_evidence"]
    assert evidence["final_tradability_state"] == "halted"
    assert evidence["tradability_decision_evidence"][0]["allowed"] is False


def test_reverse_split_preserves_total_cost_basis_on_official_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    reverse_split = _event(
        event_id="ca_btc_loader_reverse_split_0001",
        event_version_id="cav_btc_loader_reverse_split_0001_v1",
        version=1,
        event_type="reverse_split",
        effective_at="2026-01-03T00:00:00+00:00",
        observed_at="2026-01-03T00:00:00+00:00",
        ratio="0.5",
    )
    manifest = _manifest(events=[reverse_split], adjusted=False)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    run = _run_official_buy_and_hold(manifest=manifest, snapshot=snapshot)
    entry = next(
        item for item in run.ledger_entries if item.entry_type == "corporate_action"
    )

    assert entry.asset_qty_before == pytest.approx(9_000)
    assert entry.asset_qty_after == pytest.approx(4_500)
    assert entry.cost_basis_before == pytest.approx(990_000)
    assert entry.cost_basis_after == pytest.approx(990_000)
    assert entry.cash_before == pytest.approx(entry.cash_after)


def test_fractional_split_entitlement_without_cash_in_lieu_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    fractional = _event(
        event_id="ca_btc_loader_fractional_split_0001",
        event_version_id="cav_btc_loader_fractional_split_0001_v1",
        version=1,
        event_type="reverse_split",
        effective_at="2026-01-03T00:00:00+00:00",
        observed_at="2026-01-03T00:00:00+00:00",
        ratio="0.33333333",
    )
    manifest = _manifest(events=[fractional], adjusted=False)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )

    with pytest.raises(
        ValueError,
        match="fractional_entitlement_cash_in_lieu_terms_required",
    ):
        _run_official_buy_and_hold(manifest=manifest, snapshot=snapshot)


def test_trading_resume_restores_tradability_before_later_buy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    events = [
        _event(
            event_id="ca_btc_loader_halt_resume_halt_0001",
            event_version_id="cav_btc_loader_halt_resume_halt_0001_v1",
            version=1,
            event_type="trading_halt",
            effective_at="2025-12-31T00:00:00+00:00",
            observed_at="2025-12-31T00:00:00+00:00",
            tradability="halted",
        ),
        _event(
            event_id="ca_btc_loader_halt_resume_resume_0001",
            event_version_id="cav_btc_loader_halt_resume_resume_0001_v1",
            version=1,
            event_type="trading_resume",
            effective_at="2026-01-03T00:00:00+00:00",
            observed_at="2026-01-03T00:00:00+00:00",
            tradability="tradable",
        ),
    ]
    manifest = _manifest(events=events, adjusted=False)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    run = _run_official_buy_and_hold(
        manifest=manifest,
        snapshot=snapshot,
        buy_index=3,
    )

    assert len(run.fills) == 1
    assert run.fills[0].side == "BUY"
    evidence = run.execution_event_summary["corporate_action_portfolio_evidence"]
    assert evidence["final_tradability_state"] == "tradable"
    assert evidence["tradability_decision_evidence"][0]["allowed"] is True


def test_ticker_change_with_stable_instrument_mapping_is_identity_noop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    ticker_change = _event(
        event_id="ca_btc_loader_ticker_change_0001",
        event_version_id="cav_btc_loader_ticker_change_0001_v1",
        version=1,
        event_type="ticker_change",
        effective_at="2026-01-03T00:00:00+00:00",
        observed_at="2026-01-03T00:00:00+00:00",
        replacement_symbol="XBTKRW",
        replacement_instrument_id="inst_btc_internal_0001",
    )
    manifest = _manifest(events=[ticker_change], adjusted=False)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    run = _run_official_buy_and_hold(manifest=manifest, snapshot=snapshot)
    entry = next(
        item for item in run.ledger_entries if item.entry_type == "corporate_action"
    )

    assert entry.qty == pytest.approx(0)
    assert entry.cash_delta == pytest.approx(0)
    assert entry.asset_qty_before == pytest.approx(entry.asset_qty_after)
    assert entry.cost_basis_before == pytest.approx(entry.cost_basis_after)
    event = entry.corporate_action_event
    assert event is not None
    assert event["event"]["replacement_symbol"] == "XBTKRW"


@pytest.mark.parametrize(
    ("event_type", "tradability"),
    [
        ("delisting", "delisted"),
        ("etf_liquidation", "delisted"),
        ("etf_merger", None),
    ],
)
def test_terminal_cash_recovery_closes_position_and_cost_basis(
    tmp_path: Path,
    event_type: str,
    tradability: str | None,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    terminal = _event(
        event_id=f"ca_btc_loader_terminal_{event_type}_0001",
        event_version_id=f"cav_btc_loader_terminal_{event_type}_0001_v1",
        version=1,
        event_type=event_type,
        effective_at="2026-01-05T00:01:00+00:00",
        observed_at="2026-01-05T00:01:00+00:00",
        cash_amount="55",
        tradability=tradability,
    )
    manifest = _manifest(events=[terminal], adjusted=False)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    run = _run_official_buy_and_hold(manifest=manifest, snapshot=snapshot)
    entry = next(
        item for item in run.ledger_entries if item.entry_type == "corporate_action"
    )

    assert entry.asset_qty_before == pytest.approx(9_000)
    assert entry.asset_qty_after == pytest.approx(0)
    assert entry.cost_basis_before == pytest.approx(990_000)
    assert entry.cost_basis_after == pytest.approx(0)
    assert entry.cash_delta == pytest.approx(495_000)
    assert entry.realized_pnl == pytest.approx(-495_000)
    assert run.resource_usage["final_cash"] == pytest.approx(505_000)
    assert run.resource_usage["final_asset_qty"] == pytest.approx(0)
    assert len(run.closed_trades) == 1
    assert run.closed_trades[0].exit_rule == "corporate_action_terminal_recovery"
    assert run.closed_trades[0].exit_reason == event_type
    assert run.metrics.trade_count == 1


def test_terminal_recovery_after_last_candle_drains_to_declared_split_end(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM candles WHERE ts = ?", (_epoch_ms("2026-01-05"),))
        connection.commit()
    finally:
        connection.close()
    terminal = _event(
        event_id="ca_btc_loader_terminal_after_last_candle_0001",
        event_version_id="cav_btc_loader_terminal_after_last_candle_0001_v1",
        version=1,
        event_type="delisting",
        effective_at="2026-01-05T12:00:00+00:00",
        observed_at="2026-01-05T12:00:00+00:00",
        cash_amount="55",
        tradability="delisted",
    )
    manifest = _manifest(events=[terminal], adjusted=False)
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )
    run = _run_official_buy_and_hold(manifest=manifest, snapshot=snapshot)

    assert run.resource_usage["final_cash"] == pytest.approx(505_000)
    assert run.resource_usage["final_asset_qty"] == pytest.approx(0)
    assert run.closed_trades[0].exit_reason == "delisting"
    assert run.equity_curve[-1].ts == int(
        datetime.fromisoformat("2026-01-05T12:00:00+00:00").timestamp() * 1000
    )
    assert run.equity_curve[-1].mark_price_source == (
        "corporate_action_terminal_cash_recovery"
    )
    evidence = run.execution_event_summary["corporate_action_portfolio_evidence"]
    assert evidence["terminal_event_version_id"] == terminal["event_version_id"]


def test_nonterminal_economic_event_after_last_market_observation_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM candles WHERE ts = ?", (_epoch_ms("2026-01-05"),))
        connection.commit()
    finally:
        connection.close()
    split = _event(
        event_id="ca_btc_loader_split_after_last_candle_0001",
        event_version_id="cav_btc_loader_split_after_last_candle_0001_v1",
        version=1,
        event_type="split",
        effective_at="2026-01-05T12:00:00+00:00",
        observed_at="2026-01-05T12:00:00+00:00",
        ratio="2",
    )
    manifest = _manifest(events=[split], adjusted=False)

    with pytest.raises(
        CorporateActionContractError,
        match="economic_event_after_last_market_observation_unsupported",
    ):
        load_dataset_split(
            db_path=path,
            manifest=manifest,
            split_name="train",
        )


@pytest.mark.parametrize(
    ("event_type", "cash_amount", "reason"),
    [
        (
            "capital_reduction",
            None,
            "event_aware_portfolio_accounting_required",
        ),
        ("etf_merger", None, "stock_merger_conversion_unsupported"),
    ],
)
def test_unsupported_corporate_action_semantics_remain_fail_closed(
    tmp_path: Path,
    event_type: str,
    cash_amount: str | None,
    reason: str,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    unsupported = _event(
        event_id=f"ca_btc_loader_unsupported_{event_type}_0001",
        event_version_id=f"cav_btc_loader_unsupported_{event_type}_0001_v1",
        version=1,
        event_type=event_type,
        effective_at="2026-01-03T00:00:00+00:00",
        observed_at="2026-01-03T00:00:00+00:00",
        cash_amount=cash_amount,
    )
    manifest = _manifest(events=[unsupported], adjusted=False)

    with pytest.raises(CorporateActionContractError, match=reason):
        load_dataset_split(
            db_path=path,
            manifest=manifest,
            split_name="train",
        )


@pytest.mark.parametrize(
    ("split_suffix", "dividend_suffix"),
    [("aaa", "zzz"), ("zzz", "aaa")],
)
def test_same_timestamp_entitlement_never_depends_on_event_id_order(
    tmp_path: Path,
    split_suffix: str,
    dividend_suffix: str,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    events = sorted(
        [
            _event(
                event_id=f"ca_btc_same_time_split_{split_suffix}_0001",
                event_version_id=(
                    f"cav_btc_same_time_split_{split_suffix}_0001_v1"
                ),
                version=1,
                event_type="split",
                effective_at="2026-01-03T00:00:00+00:00",
                observed_at="2026-01-03T00:00:00+00:00",
                ratio="2",
            ),
            _event(
                event_id=f"ca_btc_same_time_dividend_{dividend_suffix}_0001",
                event_version_id=(
                    f"cav_btc_same_time_dividend_{dividend_suffix}_0001_v1"
                ),
                version=1,
                event_type="cash_dividend",
                effective_at="2026-01-03T00:00:00+00:00",
                observed_at="2026-01-03T00:00:00+00:00",
                cash_amount="5",
            ),
        ],
        key=lambda item: (
            str(item["effective_at"]),
            str(item["observed_at"]),
            str(item["event_id"]),
            int(item["version"]),
        ),
    )
    manifest = _manifest(events=events, adjusted=False)

    with pytest.raises(
        CorporateActionContractError,
        match="same_timestamp_event_ordering_terms_required",
    ):
        load_dataset_split(
            db_path=path,
            manifest=manifest,
            split_name="train",
        )


def test_mixed_cash_and_replacement_merger_remains_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    mixed_merger = _event(
        event_id="ca_btc_loader_mixed_merger_0001",
        event_version_id="cav_btc_loader_mixed_merger_0001_v1",
        version=1,
        event_type="etf_merger",
        effective_at="2026-01-03T00:00:00+00:00",
        observed_at="2026-01-03T00:00:00+00:00",
        cash_amount="55",
        replacement_instrument_id="inst_btc_replacement_0002",
    )
    manifest = _manifest(events=[mixed_merger], adjusted=False)

    with pytest.raises(
        CorporateActionContractError,
        match="stock_merger_conversion_unsupported",
    ):
        load_dataset_split(
            db_path=path,
            manifest=manifest,
            split_name="train",
        )


def test_sub_millisecond_late_observation_cannot_floor_into_effective_boundary() -> (
    None
):
    event = _event(
        event_id="ca_btc_loader_submillisecond_0001",
        event_version_id="cav_btc_loader_submillisecond_0001_v1",
        version=1,
        event_type="split",
        effective_at="2026-01-03T00:00:00.000000+00:00",
        observed_at="2026-01-03T00:00:00.000500+00:00",
        ratio="2",
    )

    with pytest.raises(
        CorporateActionContractError,
        match="timestamp_millisecond_alignment_required",
    ):
        _manifest(events=[event], adjusted=False)


def test_future_correction_does_not_change_applied_event_accounting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    base_manifest = _manifest(events=_action_events(), adjusted=False)
    suffixed_manifest = _manifest(
        events=_action_events(future_split_correction=True),
        adjusted=False,
    )
    base_snapshot = load_dataset_split(
        db_path=path,
        manifest=base_manifest,
        split_name="train",
    )
    suffixed_snapshot = load_dataset_split(
        db_path=path,
        manifest=suffixed_manifest,
        split_name="train",
    )
    base_run = _run_official_buy_and_hold(
        manifest=base_manifest,
        snapshot=base_snapshot,
    )
    suffixed_run = _run_official_buy_and_hold(
        manifest=suffixed_manifest,
        snapshot=suffixed_snapshot,
    )

    assert base_snapshot.candles == suffixed_snapshot.candles
    assert base_snapshot.snapshot_data_hash() != suffixed_snapshot.snapshot_data_hash()
    assert [event.decision_id() for event in base_run.decisions] == [
        event.decision_id() for event in suffixed_run.decisions
    ]
    assert base_run.equity_curve == suffixed_run.equity_curve
    assert base_run.metrics == suffixed_run.metrics
    assert [fill.filled_qty for fill in base_run.fills] == [
        fill.filled_qty for fill in suffixed_run.fills
    ]
    base_evidence = base_run.execution_event_summary[
        "corporate_action_portfolio_evidence"
    ]
    suffixed_evidence = suffixed_run.execution_event_summary[
        "corporate_action_portfolio_evidence"
    ]
    assert base_evidence["application_evidence"] == suffixed_evidence[
        "application_evidence"
    ]
    assert base_evidence["portfolio_event_plan_hash"] != suffixed_evidence[
        "portfolio_event_plan_hash"
    ]


def test_later_event_suffix_does_not_change_earlier_supported_strategy_output(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    base_manifest = _manifest(events=[], adjusted=False)
    event_manifest = _manifest(events=_action_events(), adjusted=False)
    base_snapshot = load_dataset_split(
        db_path=path,
        manifest=base_manifest,
        split_name="train",
    )
    event_snapshot = load_dataset_split(
        db_path=path,
        manifest=event_manifest,
        split_name="train",
    )
    plugin = resolve_builtin_strategy("threshold_research_only")
    common = {
        "plugin": plugin,
        "parameter_values": {"THRESHOLD_CLOSE_ABOVE": 75},
        "fee_rate": 0.0,
        "slippage_bps": 0.0,
    }
    base_run = run_common_simulation_backtest(
        **common,
        dataset=base_snapshot,
        execution_timing_policy=base_manifest.execution_timing,
        portfolio_policy=base_manifest.portfolio_policy,
        risk_policy=base_manifest.risk_policy,
    )
    event_run = run_common_simulation_backtest(
        **common,
        dataset=event_snapshot,
        execution_timing_policy=event_manifest.execution_timing,
        portfolio_policy=event_manifest.portfolio_policy,
        risk_policy=event_manifest.risk_policy,
    )

    assert base_run.decisions[0].decision_id() == event_run.decisions[0].decision_id()
    assert base_run.decisions[0].final_signal == event_run.decisions[0].final_signal
    assert base_run.decisions[0].final_signal == "BUY"
    assert base_run.equity_curve[:2] == event_run.equity_curve[:2]
    first_base_fill = base_run.fills[0]
    first_event_fill = event_run.fills[0]
    assert (
        first_base_fill.fill_status,
        first_base_fill.filled_qty,
        first_base_fill.avg_fill_price,
    ) == (
        first_event_fill.fill_status,
        first_event_fill.filled_qty,
        first_event_fill.avg_fill_price,
    )


@pytest.mark.parametrize(
    ("event_type", "effective_at", "ratio", "tradability"),
    [
        ("split", "2026-01-03T00:00:00+00:00", "2", None),
        ("delisting", "2025-12-31T00:00:00+00:00", None, "delisted"),
    ],
)
def test_raw_snapshot_rejects_late_known_historical_lifecycle_event(
    tmp_path: Path,
    event_type: str,
    effective_at: str,
    ratio: str | None,
    tradability: str | None,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    event = _event(
        event_id=f"ca_btc_loader_late_{event_type}_0001",
        event_version_id=f"cav_btc_loader_late_{event_type}_0001_v1",
        version=1,
        event_type=event_type,
        effective_at=effective_at,
        observed_at="2026-01-06T00:00:00+00:00",
        ratio=ratio,
        tradability=tradability,
    )

    with pytest.raises(
        CorporateActionContractError,
        match="event_aware_portfolio_accounting_required",
    ):
        load_dataset_split(
            db_path=path,
            manifest=_manifest(
                events=[event],
                known_at="2026-01-07T00:00:00+00:00",
                adjusted=False,
            ),
            split_name="train",
        )


def test_future_action_correction_does_not_change_stochastic_strategy_output(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    base_events = _suffix_split_events()
    suffixed_events = _suffix_split_events(future_correction=True)
    base_manifest = _manifest(
        events=base_events,
        known_at="2026-01-07T00:00:00+00:00",
        adjusted=False,
    )
    suffixed_manifest = _manifest(
        events=suffixed_events,
        known_at="2026-01-07T00:00:00+00:00",
        adjusted=False,
    )
    base = load_dataset_split(
        db_path=path,
        manifest=base_manifest,
        split_name="train",
    )
    suffixed = load_dataset_split(
        db_path=path,
        manifest=suffixed_manifest,
        split_name="train",
    )

    assert suffixed.candles == base.candles
    assert suffixed.snapshot_data_hash() != base.snapshot_data_hash()
    boundary = suffixed.candles[0].available_at_ms(interval=suffixed.interval)
    causal = CausalMarketView.from_dataset(suffixed, 0, boundary).causal_snapshot()
    assert causal.corporate_action_transformation_evidence is None
    assert causal.point_in_time_decision_evidence is None
    assert (
        base_manifest.simulation_seed_scope_hash()
        != suffixed_manifest.simulation_seed_scope_hash()
    )
    assert (
        base_manifest.causal_execution_seed_scope_hash()
        == suffixed_manifest.causal_execution_seed_scope_hash()
    )
    seed_payload = suffixed_manifest.causal_execution_seed_scope_payload()
    assert seed_payload["seed_policy"] == "causal_execution_request_scoped_v1"
    assert "dataset" not in seed_payload
    assert "corporate_action_set" not in seed_payload
    plugin = resolve_builtin_strategy("threshold_research_only")
    parameters = {"THRESHOLD_CLOSE_ABOVE": 75}
    scenario = ExecutionScenario(
        type="stress",
        fee_rate=0,
        slippage_bps=0,
        partial_fill_rate=0.6,
        seed=17,
    )
    scenario_id = "scenario_corporate_action_suffix_invariance"

    def execution_model(manifest: ExperimentManifest):
        return _execution_model_from_scenario(
            scenario,
            seed_context=_seed_context(
                causal_execution_seed_scope_hash=(
                    manifest.causal_execution_seed_scope_hash()
                ),
                scenario=scenario,
                scenario_id=scenario_id,
                parameter_candidate_id="candidate_corporate_action_suffix",
                split_name="train",
            ),
        )

    base_run = run_common_simulation_backtest(
        plugin=plugin,
        dataset=base,
        parameter_values=parameters,
        fee_rate=scenario.fee_rate,
        slippage_bps=scenario.slippage_bps,
        execution_model=execution_model(base_manifest),
        execution_timing_policy=base_manifest.execution_timing,
        portfolio_policy=base_manifest.portfolio_policy,
        risk_policy=base_manifest.risk_policy,
    )
    suffixed_run = run_common_simulation_backtest(
        plugin=plugin,
        dataset=suffixed,
        parameter_values=parameters,
        fee_rate=scenario.fee_rate,
        slippage_bps=scenario.slippage_bps,
        execution_model=execution_model(suffixed_manifest),
        execution_timing_policy=suffixed_manifest.execution_timing,
        portfolio_policy=suffixed_manifest.portfolio_policy,
        risk_policy=suffixed_manifest.risk_policy,
    )

    assert [item.decision_id() for item in suffixed_run.decisions] == [
        item.decision_id() for item in base_run.decisions
    ]
    assert base_run.decisions[0].final_signal == "BUY"
    assert [item.derived_seed_hash for item in suffixed_run.fills] == [
        item.derived_seed_hash for item in base_run.fills
    ]
    assert [item.fill_status for item in suffixed_run.fills] == [
        item.fill_status for item in base_run.fills
    ]
    economic_trade_fields = ("ts", "side", "price", "qty", "fee", "cash", "asset_qty")
    assert [
        tuple(item[field] for field in economic_trade_fields)
        for item in suffixed_run.trades
    ] == [
        tuple(item[field] for field in economic_trade_fields)
        for item in base_run.trades
    ]
    assert suffixed_run.equity_curve == base_run.equity_curve
    assert suffixed_run.metrics == base_run.metrics


@pytest.mark.parametrize(
    "known_at,reason",
    [
        (None, "corporate_action_known_at_required"),
        ("2026-01-05", "corporate_action_known_at_timezone_required"),
        (
            "2026-01-07T00:00:00.000500+00:00",
            "corporate_action_known_at_millisecond_alignment_required",
        ),
    ],
)
def test_nonempty_action_set_requires_valid_hash_bound_known_at(
    tmp_path: Path,
    known_at: str | None,
    reason: str,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    with pytest.raises(CorporateActionContractError, match=reason):
        load_dataset_split(
            db_path=path,
            manifest=_manifest(known_at=known_at),
            split_name="train",
        )


def test_nonempty_action_authority_must_cover_snapshot_end(tmp_path: Path) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)

    with pytest.raises(
        CorporateActionContractError,
        match="corporate_action_known_at_before_snapshot_end",
    ):
        load_dataset_split(
            db_path=path,
            manifest=_manifest(
                events=_suffix_split_events(),
                known_at="2026-01-05T00:00:00+00:00",
                adjusted=False,
            ),
            split_name="train",
        )


def test_official_loader_rejects_rows_on_or_after_known_delisting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path, through_day_six=True)
    delisting = _event(
        event_id="ca_btc_loader_delisting_0001",
        event_version_id="cav_btc_loader_delisting_0001_v1",
        version=1,
        event_type="delisting",
        effective_at="2026-01-06T00:00:00+00:00",
        observed_at="2026-01-06T00:00:00+00:00",
        tradability="delisted",
    )
    with pytest.raises(CorporateActionContractError, match="post_delisting"):
        load_dataset_split(
            db_path=path,
            manifest=_manifest(
                events=sorted(
                    [*_action_events(), delisting],
                    key=lambda item: (
                        str(item["effective_at"]),
                        str(item["observed_at"]),
                        str(item["event_id"]),
                        int(item["version"]),
                    ),
                ),
                known_at="2026-01-07T00:00:00+00:00",
                adjusted=False,
                end="2026-01-06",
            ),
            split_name="train",
        )


@pytest.mark.parametrize("execution_evidence", ["top_of_book", "depth"])
def test_adjusted_candles_reject_raw_quote_or_depth_scale_mixing(
    tmp_path: Path,
    execution_evidence: str,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    payload = copy.deepcopy(_manifest_with_domain_contracts())
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset["train"] = {"start": "2026-01-01", "end": "2026-01-05"}
    dataset["validation"] = {"start": "2026-01-10", "end": "2026-01-10"}
    dataset[execution_evidence] = {"required": False}
    action_set_payload = {
        "schema_version": 1,
        "instrument_id": "inst_btc_internal_0001",
        "action_set_id": "cas_btc_loader_actions_0001",
        "events": _action_events(),
    }
    action_set = parse_corporate_action_set(
        action_set_payload,
        expected_instrument_id="inst_btc_internal_0001",
    )
    payload["corporate_action_set"] = action_set_payload
    payload["corporate_action_policy"] = {
        "schema_version": 1,
        "policy_id": "cap_loader_adjusted_0001",
        "version": 1,
        "price_series": "pre_adjusted",
        "price_adjustment": "backward_total_return",
        "volume_adjustment": "inverse_split_factor",
        "dividend_treatment": "included_in_total_return_adjustment",
        "action_set_hash": action_set.contract_hash(),
    }
    manifest = parse_builtin_manifest(payload)

    with pytest.raises(CorporateActionContractError, match="scale_mismatch"):
        load_dataset_split(db_path=path, manifest=manifest, split_name="train")


def test_builtin_empty_action_set_preserves_raw_snapshot_data_semantics(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candles.sqlite"
    _write_candles(path)
    manifest = parse_builtin_manifest(_manifest_payload())
    snapshot = load_dataset_split(
        db_path=path,
        manifest=manifest,
        split_name="train",
    )

    assert snapshot.corporate_action_transformation_evidence is None
    expected = calculate_snapshot_data_hash(
        candle_rows=(candle.as_tuple() for candle in snapshot.candles),
        execution_evidence={
            "top_of_book": [],
            "top_of_book_event_quotes": [],
            "orderbook_depth_snapshots": [],
        },
    )
    assert snapshot.snapshot_data_hash() == expected
