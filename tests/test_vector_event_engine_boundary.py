from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import (
    DateRange,
    ExperimentManifest,
    legacy_research_portfolio_policy,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.vector_screening import (
    VECTOR_SCREENING_PURPOSE,
    VectorScreeningError,
    assert_vector_event_decision_parity,
    run_vector_signal_screen,
)
from market_research.research_composition import (
    builtin_strategy_registry,
    load_builtin_manifest,
    resolve_builtin_strategy,
)
from tests.research_buy_and_hold_success_fixture import create_success_fixture


def _manifest(
    tmp_path: Path,
    *,
    classification: str = "exploratory",
    include_final_holdout: bool = False,
) -> ExperimentManifest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _, manifest_path = create_success_fixture(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["research_classification"] = classification
    if include_final_holdout:
        payload["dataset"]["final_holdout"] = {
            "start": "2026-01-03",
            "end": "2026-01-03",
        }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return load_builtin_manifest(str(manifest_path))


def _dataset(
    *,
    split_name: str = "train",
    day: str = "2026-01-01",
    declared_day: str | None = None,
    extra: int = 0,
    snapshot_id: str = "unit",
) -> DatasetSnapshot:
    closes = (100.0, 101.0, 102.0, 103.0, 104.0) + tuple(
        105.0 + index for index in range(extra)
    )
    base = int(
        datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000
    )
    return DatasetSnapshot(
        snapshot_id=snapshot_id,
        source="sqlite_candles",
        market="KRW-BTC",
        interval="1m",
        split_name=split_name,
        date_range=DateRange(declared_day or day, declared_day or day),
        candles=tuple(
            Candle(base + index * 60_000, close, close + 1, close - 1, close, 1.0)
            for index, close in enumerate(closes)
        ),
    )


def _parameters(*, buy_index: int = 1) -> dict[str, object]:
    return {
        "BUY_HOLD_BUY_INDEX": buy_index,
        "BUY_HOLD_DECISION_REASON": "golden_buy_and_hold",
    }


def _screen(
    *,
    manifest: ExperimentManifest,
    dataset: DatasetSnapshot,
    buy_index: int = 1,
    horizon: int = 1,
):
    registry = builtin_strategy_registry()
    plugin = resolve_builtin_strategy("buy_and_hold_baseline")
    return run_vector_signal_screen(
        manifest=manifest,
        registry=registry,
        plugin=plugin,
        dataset=dataset,
        parameter_values=_parameters(buy_index=buy_index),
        execution_timing_policy=manifest.execution_timing,
        forward_horizon_bars=horizon,
    )


def _event_run(
    *,
    manifest: ExperimentManifest,
    dataset: DatasetSnapshot,
    buy_index: int = 1,
):
    registry = builtin_strategy_registry()
    plugin = resolve_builtin_strategy("buy_and_hold_baseline")
    return run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        dataset=dataset,
        parameter_values=_parameters(buy_index=buy_index),
        fee_rate=float(manifest.cost_model.fee_rate),
        slippage_bps=float(manifest.cost_model.slippage_bps[0]),
        execution_timing_policy=manifest.execution_timing,
        portfolio_policy=legacy_research_portfolio_policy(),
    )


def test_vector_screen_is_signal_only_and_matches_event_engine_decisions(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "manifest")
    dataset = _dataset()
    screening = _screen(manifest=manifest, dataset=dataset)
    event_run = _event_run(manifest=manifest, dataset=dataset)

    assert screening.purpose == VECTOR_SCREENING_PURPOSE
    assert "fills" not in screening.as_dict()
    assert "cash" not in screening.as_dict()
    assert "positions" not in screening.as_dict()
    assert len(screening.signals) == len(dataset.candles)
    assert screening.labels[0].label_available_at_ts > screening.signals[0].decision_ts
    assert_vector_event_decision_parity(
        screening=screening, event_run=event_run, manifest=manifest
    )


def test_vector_screen_requires_exploratory_authoritative_manifest(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "manifest", classification="research_only")
    registry = builtin_strategy_registry()
    with pytest.raises(
        VectorScreeningError,
        match="vector_screening_requires_exploratory_classification",
    ):
        run_vector_signal_screen(
            manifest=manifest,
            registry=registry,
            plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
            dataset=_dataset(),
            parameter_values=_parameters(),
        )


def test_final_holdout_relabeling_cannot_bypass_manifest_binding(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "manifest", include_final_holdout=True)
    registry = builtin_strategy_registry()
    plugin = resolve_builtin_strategy("buy_and_hold_baseline")
    with pytest.raises(VectorScreeningError, match="final_holdout_forbidden"):
        run_vector_signal_screen(
            manifest=manifest,
            registry=registry,
            plugin=plugin,
            dataset=_dataset(split_name="final_holdout", day="2026-01-03"),
            parameter_values=_parameters(),
        )
    with pytest.raises(VectorScreeningError, match="manifest_dataset_mismatch"):
        run_vector_signal_screen(
            manifest=manifest,
            registry=registry,
            plugin=plugin,
            dataset=_dataset(split_name="train", day="2026-01-03"),
            parameter_values=_parameters(),
        )
    with pytest.raises(VectorScreeningError, match="candle_outside_manifest_split"):
        run_vector_signal_screen(
            manifest=manifest,
            registry=registry,
            plugin=plugin,
            dataset=_dataset(
                split_name="train",
                day="2026-01-03",
                declared_day="2026-01-01",
            ),
            parameter_values=_parameters(),
        )


def test_horizon_is_exact_positive_int_and_bound_without_labels(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "manifest")
    dataset = _dataset()
    with pytest.raises(VectorScreeningError, match="positive_int"):
        _screen(manifest=manifest, dataset=dataset, horizon=1.5)  # type: ignore[arg-type]

    first = _screen(manifest=manifest, dataset=dataset, horizon=10)
    second = _screen(manifest=manifest, dataset=dataset, horizon=11)
    assert first.labels == second.labels == ()
    assert first.forward_horizon_bars == 10
    assert first.content_hash != second.content_hash


def test_future_suffix_does_not_change_existing_vector_decisions(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "manifest")
    original = _screen(manifest=manifest, dataset=_dataset())
    extended = _screen(manifest=manifest, dataset=_dataset(extra=3))

    assert original.signals == extended.signals[: len(original.signals)]
    assert replace(original.labels[0]) == extended.labels[0]


def test_parameter_candidate_must_be_authorized_and_materialized_by_manifest(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "manifest")
    registry = builtin_strategy_registry()
    plugin = resolve_builtin_strategy("buy_and_hold_baseline")
    with pytest.raises(VectorScreeningError, match="parameter_set_mismatch"):
        run_vector_signal_screen(
            manifest=manifest,
            registry=registry,
            plugin=plugin,
            dataset=_dataset(),
            parameter_values={"BUY_HOLD_BUY_INDEX": 1},
        )
    with pytest.raises(VectorScreeningError, match="outside_manifest"):
        run_vector_signal_screen(
            manifest=manifest,
            registry=registry,
            plugin=plugin,
            dataset=_dataset(),
            parameter_values=_parameters(buy_index=2),
        )


def test_result_and_signal_semantics_cannot_be_forged(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "manifest")
    screening = _screen(manifest=manifest, dataset=_dataset())
    with pytest.raises(VectorScreeningError, match="decision_id_content_mismatch"):
        replace(screening.signals[0], raw_signal="SELL")
    with pytest.raises(VectorScreeningError, match="split_binding_hash_mismatch"):
        replace(screening, dataset_split_name="validation")
    with pytest.raises(VectorScreeningError, match="content_hash_mismatch"):
        replace(screening, content_hash="sha256:" + "f" * 64)


def test_parity_fails_closed_on_mismatched_inputs_or_decision_stream(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "manifest")
    dataset = _dataset()
    screening = _screen(manifest=manifest, dataset=dataset)
    parameter_mismatch = _event_run(manifest=manifest, dataset=dataset, buy_index=2)
    with pytest.raises(VectorScreeningError, match="authoritative_input_mismatch"):
        assert_vector_event_decision_parity(
            screening=screening,
            event_run=parameter_mismatch,
            manifest=manifest,
        )

    dataset_mismatch = _event_run(
        manifest=manifest,
        dataset=_dataset(snapshot_id="different-snapshot"),
    )
    with pytest.raises(VectorScreeningError, match="authoritative_input_mismatch"):
        assert_vector_event_decision_parity(
            screening=screening,
            event_run=dataset_mismatch,
            manifest=manifest,
        )

    event_run = _event_run(manifest=manifest, dataset=dataset)
    forged_run = replace(event_run, decision_stream_hash="sha256:" + "e" * 64)
    with pytest.raises(
        VectorScreeningError, match="authoritative_decision_ids_mismatch"
    ):
        assert_vector_event_decision_parity(
            screening=screening, event_run=forged_run, manifest=manifest
        )
