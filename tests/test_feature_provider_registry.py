from __future__ import annotations

import pytest

from bithumb_bot.research.feature_diagnostic_features import SmaGapProvider, feature_provider_for_name
from bithumb_bot.research.feature_provider_registry import (
    CATEGORY_FEATURE_VALUE_TYPES,
    NUMERIC_FEATURE_VALUE_TYPES,
    _definition_hash,
    feature_provider_spec_for_name,
    list_feature_provider_specs,
)


def test_all_provider_names_are_unique() -> None:
    names = [spec.name for spec in list_feature_provider_specs()]

    assert len(names) == len(set(names))


def test_all_providers_declare_value_type() -> None:
    for spec in list_feature_provider_specs():
        assert spec.value_type in {"float", "str", "bool"}


def test_all_providers_declare_bucketizer_type() -> None:
    for spec in list_feature_provider_specs():
        assert spec.bucketizer_type in {"quantile", "category"}


def test_all_provider_specs_have_bucketizer_compatible_value_type() -> None:
    for spec in list_feature_provider_specs():
        if spec.bucketizer_type == "quantile":
            assert spec.value_type in NUMERIC_FEATURE_VALUE_TYPES
        elif spec.bucketizer_type == "category":
            assert spec.value_type in CATEGORY_FEATURE_VALUE_TYPES
        else:
            raise AssertionError(f"unexpected bucketizer_type={spec.bucketizer_type!r}")


def test_regime_provider_spec_declares_category_universe() -> None:
    spec = feature_provider_spec_for_name("regime")

    assert spec.bucketizer_type == "category"
    assert "uptrend_normal_vol_volume_normal" in spec.category_universe
    assert "sideways_low_vol_volume_decreasing" in spec.category_universe


def test_provider_definition_hash_changes_when_parameters_change() -> None:
    first = _definition_hash(SmaGapProvider(short_window=5, long_window=20))
    second = _definition_hash(SmaGapProvider(short_window=10, long_window=50))

    assert first != second


def test_unknown_feature_name_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown diagnostic feature"):
        feature_provider_spec_for_name("unknown_feature")


def test_legacy_provider_lookup_uses_registry_and_fails_closed() -> None:
    assert feature_provider_for_name("sma_gap").name == "sma_gap"
    with pytest.raises(ValueError, match="unknown diagnostic feature"):
        feature_provider_for_name("unknown_feature")
