from __future__ import annotations

import pytest

from bithumb_bot.research.split_usage_policy import (
    FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK,
    FINAL_HOLDOUT_DIAGNOSTIC_OVERRIDE_REQUIRED,
    SplitUsagePolicyError,
    validate_split_usage,
)


def test_feature_mining_final_holdout_requires_explicit_override() -> None:
    with pytest.raises(SplitUsagePolicyError) as exc:
        validate_split_usage(
            split_name="final_holdout",
            purpose="feature_mining",
            explicit_override=False,
        )

    assert exc.value.reason == FINAL_HOLDOUT_DIAGNOSTIC_OVERRIDE_REQUIRED
    assert exc.value.split_name == "final_holdout"


def test_feature_mining_train_is_allowed_without_override() -> None:
    assert (
        validate_split_usage(split_name="train", purpose="feature_mining", explicit_override=False)
        == ()
    )


def test_feature_mining_validation_is_allowed_without_override() -> None:
    assert (
        validate_split_usage(split_name="validation", purpose="feature_mining", explicit_override=False)
        == ()
    )


def test_final_holdout_override_returns_contamination_warning() -> None:
    warnings = validate_split_usage(
        split_name="final_holdout",
        purpose="feature_mining",
        explicit_override=True,
    )

    assert warnings == (
        {
            "reason": FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK,
            "split_name": "final_holdout",
        },
    )
