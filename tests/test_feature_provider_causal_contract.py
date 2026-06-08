from __future__ import annotations

import ast
import inspect
from pathlib import Path

from bithumb_bot.research.dataset_snapshot import Candle
from bithumb_bot.research.feature_diagnostic_features import AsOfCandleView, FeatureProvider
from bithumb_bot.research.feature_provider_registry import list_feature_provider_specs


ROOT = Path(__file__).resolve().parents[1]


def _candles(count: int = 80) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            ts=index,
            open=100.0 + index,
            high=102.0 + index,
            low=99.0 + index,
            close=101.0 + index,
            volume=10.0 + index,
        )
        for index in range(count)
    )


def _extreme_future(candles: tuple[Candle, ...], *, index: int) -> tuple[Candle, ...]:
    changed = list(candles)
    for future_index in range(index + 1, len(changed)):
        changed[future_index] = Candle(
            ts=changed[future_index].ts,
            open=1.0,
            high=1_000_000.0,
            low=0.01,
            close=500_000.0,
            volume=9_999_999.0,
        )
    return tuple(changed)


def test_all_registered_feature_providers_are_future_perturbation_invariant() -> None:
    candles = _candles()
    for spec in list_feature_provider_specs():
        index = max(spec.required_history + 2, 25)
        baseline = spec.provider.compute(view=AsOfCandleView(candles=candles, index=index))
        changed = spec.provider.compute(view=AsOfCandleView(candles=_extreme_future(candles, index=index), index=index))

        assert baseline == changed, spec.name


def test_provider_output_does_not_change_when_future_ohlcv_is_extreme() -> None:
    candles = _candles()
    index = 30
    mutated = _extreme_future(candles, index=index)

    for spec in list_feature_provider_specs():
        assert spec.provider.compute(view=AsOfCandleView(candles=candles, index=index)) == spec.provider.compute(
            view=AsOfCandleView(candles=mutated, index=index)
        )


def test_no_registered_provider_has_causal_contract_exemption_by_default() -> None:
    for spec in list_feature_provider_specs():
        assert spec.causal_contract_exemption_reason is None


def test_feature_provider_protocol_does_not_accept_snapshot_or_raw_candles() -> None:
    signature = inspect.signature(FeatureProvider.compute)
    parameters = signature.parameters

    assert tuple(parameters) == ("self", "view")
    assert "candles" not in parameters
    assert "snapshot" not in parameters
    assert "targets" not in parameters


def test_feature_modules_do_not_import_forward_targets() -> None:
    for relative in (
        "src/bithumb_bot/research/feature_diagnostic_features.py",
        "src/bithumb_bot/research/feature_provider_registry.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bithumb_bot.research.forward_targets":
                imported = {alias.name for alias in node.names}
                assert not {
                    "ForwardTarget",
                    "forward_targets",
                    "compute_forward_target",
                    "compute_forward_targets",
                } & imported
