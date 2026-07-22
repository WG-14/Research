"""Fail-closed temporal-validation contracts for offline research.

The contracts in this module make label horizons and every exclusion around a
train/test boundary explicit.  They are deliberately independent of any
strategy implementation: callers supply immutable date ranges, and the module
returns a recursively immutable, content-hashed nested validation plan.

No market data is loaded here.  A plan is an admission contract that can be
bound to an externally prepared dataset authority before a walk-forward run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from .hashing import sha256_prefixed


TEMPORAL_VALIDATION_SCHEMA_VERSION = 1
TEMPORAL_VALIDATION_HASH_LABEL = "nested_temporal_validation_plan"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


class TemporalValidationError(ValueError):
    """A temporal validation declaration or plan is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class NestedTemporalValidationConfig:
    """Predeclared purge, embargo, and inner-validation policy.

    ``purge_days`` must cover the complete label horizon.  Because this is a
    strictly forward-only contract, ``embargo_days`` means an additional
    *pre-test* exclusion after the purge range; it is not the post-test embargo
    used by non-causal/combinatorial CV.  Requiring at least two inner folds
    ensures that a declaration described as nested is not merely a renamed
    single split.
    """

    schema_version: int
    label_horizon_days: int
    purge_days: int
    embargo_days: int
    inner_fold_count: int
    inner_test_window_days: int
    min_inner_train_window_days: int

    def __post_init__(self) -> None:
        if self.schema_version != TEMPORAL_VALIDATION_SCHEMA_VERSION:
            raise TemporalValidationError(
                "temporal_validation_schema_version_unsupported"
            )
        _require_int_at_least(self.label_horizon_days, 1, "label_horizon_days")
        _require_int_at_least(self.purge_days, 1, "purge_days")
        if self.purge_days < self.label_horizon_days:
            raise TemporalValidationError(
                "temporal_validation_purge_shorter_than_label_horizon"
            )
        _require_int_at_least(self.embargo_days, 1, "embargo_days")
        _require_int_at_least(self.inner_fold_count, 2, "inner_fold_count")
        _require_int_at_least(self.inner_test_window_days, 1, "inner_test_window_days")
        _require_int_at_least(
            self.min_inner_train_window_days,
            1,
            "min_inner_train_window_days",
        )

    @property
    def boundary_exclusion_days(self) -> int:
        return self.purge_days + self.embargo_days

    def as_dict(self) -> dict[str, int]:
        return {
            "schema_version": self.schema_version,
            "label_horizon_days": self.label_horizon_days,
            "purge_days": self.purge_days,
            "embargo_days": self.embargo_days,
            "inner_fold_count": self.inner_fold_count,
            "inner_test_window_days": self.inner_test_window_days,
            "min_inner_train_window_days": self.min_inner_train_window_days,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(), label="nested_temporal_validation_config"
        )


@dataclass(frozen=True, slots=True)
class TemporalDateRange:
    start: str
    end: str

    def __post_init__(self) -> None:
        start = _parse_date(self.start, "temporal_range.start")
        end = _parse_date(self.end, "temporal_range.end")
        if end < start:
            raise TemporalValidationError("temporal_range_order_invalid")

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end}

    @property
    def start_date(self) -> date:
        return _parse_date(self.start, "temporal_range.start")

    @property
    def end_date(self) -> date:
        return _parse_date(self.end, "temporal_range.end")

    @property
    def day_count(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def contains(self, value: date) -> bool:
        return self.start_date <= value <= self.end_date


@dataclass(frozen=True, slots=True)
class TemporalLabelInterval:
    """One immutable label interval keyed by its feature observation date."""

    sample_id: str
    observation_date: str
    label_start: str
    label_end: str

    def __post_init__(self) -> None:
        _require_id(self.sample_id, "temporal_label.sample_id")
        observation = _parse_date(
            self.observation_date, "temporal_label.observation_date"
        )
        start = _parse_date(self.label_start, "temporal_label.label_start")
        end = _parse_date(self.label_end, "temporal_label.label_end")
        if observation > start or start > end:
            raise TemporalValidationError("temporal_label_interval_order_invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "sample_id": self.sample_id,
            "observation_date": self.observation_date,
            "label_start": self.label_start,
            "label_end": self.label_end,
        }

    def interval_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="temporal_label_interval")


@dataclass(frozen=True, slots=True)
class PurgedTemporalSplit:
    """A train/test split with named, non-overlapping exclusion ranges."""

    split_id: str
    train: TemporalDateRange
    purge: TemporalDateRange
    embargo: TemporalDateRange
    test: TemporalDateRange

    def __post_init__(self) -> None:
        _require_id(self.split_id, "temporal_split.split_id")
        if self.train.end_date + timedelta(days=1) != self.purge.start_date:
            raise TemporalValidationError("temporal_split_purge_not_contiguous")
        if self.purge.end_date + timedelta(days=1) != self.embargo.start_date:
            raise TemporalValidationError("temporal_split_embargo_not_contiguous")
        if self.embargo.end_date + timedelta(days=1) != self.test.start_date:
            raise TemporalValidationError("temporal_split_test_not_contiguous")

    def as_dict(self) -> dict[str, object]:
        return {
            "split_id": self.split_id,
            "train": self.train.as_dict(),
            "purge": self.purge.as_dict(),
            "embargo": self.embargo.as_dict(),
            "test": self.test.as_dict(),
        }

    def split_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="purged_temporal_split")


@dataclass(frozen=True, slots=True)
class NestedTemporalFold:
    """One outer split and its predeclared expanding-window inner splits."""

    outer_split: PurgedTemporalSplit
    inner_splits: tuple[PurgedTemporalSplit, ...]

    def __post_init__(self) -> None:
        if not self.inner_splits:
            raise TemporalValidationError("nested_temporal_inner_splits_required")
        outer_match = re.fullmatch(r"outer_(\d{3})", self.outer_split.split_id)
        if outer_match is None:
            raise TemporalValidationError("nested_temporal_outer_split_id_invalid")
        ids = tuple(item.split_id for item in self.inner_splits)
        expected_ids = tuple(
            f"inner_{outer_match.group(1)}_{index:03d}"
            for index in range(1, len(self.inner_splits) + 1)
        )
        if ids != expected_ids:
            raise TemporalValidationError("nested_temporal_inner_split_ids_invalid")
        outer_train = self.outer_split.train
        previous_test_end: date | None = None
        for split in self.inner_splits:
            for range_ in (split.train, split.purge, split.embargo, split.test):
                if not (
                    outer_train.contains(range_.start_date)
                    and outer_train.contains(range_.end_date)
                ):
                    raise TemporalValidationError(
                        "nested_temporal_inner_split_outside_outer_train"
                    )
            if split.train.start_date != outer_train.start_date:
                raise TemporalValidationError(
                    "nested_temporal_inner_train_start_mismatch"
                )
            if (
                previous_test_end is not None
                and split.test.start_date <= previous_test_end
            ):
                raise TemporalValidationError(
                    "nested_temporal_inner_test_ranges_overlap"
                )
            previous_test_end = split.test.end_date
        if self.inner_splits[-1].test.end_date != outer_train.end_date:
            raise TemporalValidationError(
                "nested_temporal_inner_test_coverage_incomplete"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "outer_split": self.outer_split.as_dict(),
            "inner_splits": [item.as_dict() for item in self.inner_splits],
        }

    def fold_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="nested_temporal_fold")


@dataclass(frozen=True, slots=True)
class NestedTemporalValidationPlan:
    """Recursively immutable and hash-bound nested temporal validation plan."""

    schema_version: int
    source_binding_hash: str
    config: NestedTemporalValidationConfig
    label_intervals: tuple[TemporalLabelInterval, ...]
    outer_folds: tuple[NestedTemporalFold, ...]

    def __post_init__(self) -> None:
        if self.schema_version != TEMPORAL_VALIDATION_SCHEMA_VERSION:
            raise TemporalValidationError(
                "temporal_validation_plan_schema_version_unsupported"
            )
        _require_hash(self.source_binding_hash, "source_binding_hash")
        if not self.label_intervals:
            raise TemporalValidationError("temporal_label_intervals_required")
        label_keys = tuple(
            (item.observation_date, item.sample_id) for item in self.label_intervals
        )
        if len(label_keys) != len(set(label_keys)) or label_keys != tuple(
            sorted(label_keys)
        ):
            raise TemporalValidationError("temporal_label_intervals_not_unique_sorted")
        if not self.outer_folds:
            raise TemporalValidationError("nested_temporal_outer_folds_required")
        outer_ids = tuple(fold.outer_split.split_id for fold in self.outer_folds)
        expected_outer_ids = tuple(
            f"outer_{index:03d}" for index in range(1, len(self.outer_folds) + 1)
        )
        if outer_ids != expected_outer_ids:
            raise TemporalValidationError("nested_temporal_outer_fold_ids_invalid")
        if any(
            len(fold.inner_splits) != self.config.inner_fold_count
            for fold in self.outer_folds
        ):
            raise TemporalValidationError("nested_temporal_inner_fold_count_mismatch")
        self._validate_label_interval_calendar()
        self._validate_exclusions_and_leakage()

    @property
    def label_intervals_hash(self) -> str:
        return sha256_prefixed(
            [item.as_dict() for item in self.label_intervals],
            label="temporal_label_intervals",
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_binding_hash": self.source_binding_hash,
            "config": self.config.as_dict(),
            "config_hash": self.config.contract_hash(),
            "label_intervals": [item.as_dict() for item in self.label_intervals],
            "label_intervals_hash": self.label_intervals_hash,
            "outer_folds": [item.as_dict() for item in self.outer_folds],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(
            self.canonical_payload(), label=TEMPORAL_VALIDATION_HASH_LABEL
        )

    def as_dict(self) -> dict[str, object]:
        return {**self.canonical_payload(), "content_hash": self.contract_hash()}

    def outer_windows(self) -> tuple[dict[str, dict[str, str]], ...]:
        """Return the admitted, exclusion-adjusted outer execution ranges."""

        return tuple(
            {
                "train": fold.outer_split.train.as_dict(),
                "test": fold.outer_split.test.as_dict(),
            }
            for fold in self.outer_folds
        )

    def _validate_exclusions_and_leakage(self) -> None:
        previous_test_end: date | None = None
        for fold in self.outer_folds:
            outer = fold.outer_split
            self._validate_split(outer, is_inner=False)
            if (
                previous_test_end is not None
                and outer.test.start_date <= previous_test_end
            ):
                raise TemporalValidationError(
                    "nested_temporal_outer_test_ranges_overlap"
                )
            previous_test_end = outer.test.end_date
            for inner in fold.inner_splits:
                self._validate_split(inner, is_inner=True)

    def _validate_label_interval_calendar(self) -> None:
        first = min(fold.outer_split.train.start_date for fold in self.outer_folds)
        last = max(fold.outer_split.test.end_date for fold in self.outer_folds)
        expected_dates: list[date] = []
        cursor = first
        while cursor <= last:
            expected_dates.append(cursor)
            cursor += timedelta(days=1)
        observed_dates = [
            _parse_date(item.observation_date, "temporal_label.observation_date")
            for item in self.label_intervals
        ]
        if observed_dates != expected_dates:
            raise TemporalValidationError("temporal_label_interval_calendar_incomplete")
        for interval, observation in zip(
            self.label_intervals, expected_dates, strict=True
        ):
            if interval.sample_id != f"calendar-day:{observation.isoformat()}":
                raise TemporalValidationError("temporal_label_sample_id_mismatch")
            if interval.label_start != observation.isoformat():
                raise TemporalValidationError("temporal_label_start_mismatch")
            expected_end = observation + timedelta(days=self.config.label_horizon_days)
            if interval.label_end != expected_end.isoformat():
                raise TemporalValidationError("temporal_label_horizon_mismatch")

    def _validate_split(self, split: PurgedTemporalSplit, *, is_inner: bool) -> None:
        if split.purge.day_count != self.config.purge_days:
            raise TemporalValidationError("temporal_split_purge_length_mismatch")
        if split.embargo.day_count != self.config.embargo_days:
            raise TemporalValidationError("temporal_split_embargo_length_mismatch")
        if is_inner:
            if split.test.day_count != self.config.inner_test_window_days:
                raise TemporalValidationError(
                    "nested_temporal_inner_test_length_mismatch"
                )
            if split.train.day_count < self.config.min_inner_train_window_days:
                raise TemporalValidationError("nested_temporal_inner_train_too_short")
        test_start = split.test.start_date
        for interval in self.label_intervals:
            observation = _parse_date(
                interval.observation_date, "temporal_label.observation_date"
            )
            if not split.train.contains(observation):
                continue
            label_end = _parse_date(interval.label_end, "temporal_label.label_end")
            if label_end >= test_start:
                raise TemporalValidationError(
                    f"temporal_label_leakage:{split.split_id}:{interval.sample_id}"
                )


def parse_nested_temporal_validation_config(
    value: Mapping[str, Any],
) -> NestedTemporalValidationConfig:
    if not isinstance(value, Mapping):
        raise TemporalValidationError("temporal_validation_config_must_be_object")
    expected = {
        "schema_version",
        "label_horizon_days",
        "purge_days",
        "embargo_days",
        "inner_fold_count",
        "inner_test_window_days",
        "min_inner_train_window_days",
    }
    _require_exact_fields(value, expected, "temporal_validation_config")
    return NestedTemporalValidationConfig(
        schema_version=_required_int(value, "schema_version"),
        label_horizon_days=_required_int(value, "label_horizon_days"),
        purge_days=_required_int(value, "purge_days"),
        embargo_days=_required_int(value, "embargo_days"),
        inner_fold_count=_required_int(value, "inner_fold_count"),
        inner_test_window_days=_required_int(value, "inner_test_window_days"),
        min_inner_train_window_days=_required_int(value, "min_inner_train_window_days"),
    )


def build_nested_temporal_validation_plan(
    *,
    windows: Sequence[Mapping[str, Any]],
    source_binding_hash: str,
    config: NestedTemporalValidationConfig,
) -> NestedTemporalValidationPlan:
    """Build and validate a deterministic nested forward-validation plan.

    Each input window must contain adjacent ``train`` and ``test`` date ranges.
    Purge and embargo days are carved out of the end of the input train range;
    the returned outer train range is therefore the only range admitted for
    strategy fitting or parameter selection.
    """

    _require_hash(source_binding_hash, "source_binding_hash")
    if not windows:
        raise TemporalValidationError("nested_temporal_source_windows_required")
    parsed_windows = tuple(_parse_source_window(item) for item in windows)
    folds: list[NestedTemporalFold] = []
    for index, (raw_train, test) in enumerate(parsed_windows, start=1):
        if raw_train.end_date + timedelta(days=1) != test.start_date:
            raise TemporalValidationError("nested_temporal_source_window_not_adjacent")
        outer = _build_purged_split(
            split_id=f"outer_{index:03d}",
            raw_train_start=raw_train.start_date,
            test_start=test.start_date,
            test_end=test.end_date,
            config=config,
        )
        inner = _build_inner_splits(outer=outer, config=config, outer_index=index)
        folds.append(NestedTemporalFold(outer_split=outer, inner_splits=inner))

    start = min(item[0].start_date for item in parsed_windows)
    end = max(item[1].end_date for item in parsed_windows)
    labels = build_calendar_label_intervals(
        start=start.isoformat(),
        end=end.isoformat(),
        label_horizon_days=config.label_horizon_days,
    )
    return NestedTemporalValidationPlan(
        schema_version=TEMPORAL_VALIDATION_SCHEMA_VERSION,
        source_binding_hash=source_binding_hash,
        config=config,
        label_intervals=labels,
        outer_folds=tuple(folds),
    )


def build_calendar_label_intervals(
    *, start: str, end: str, label_horizon_days: int
) -> tuple[TemporalLabelInterval, ...]:
    """Build explicit daily label intervals for a declared outcome horizon."""

    _require_int_at_least(label_horizon_days, 1, "label_horizon_days")
    first = _parse_date(start, "label_calendar.start")
    last = _parse_date(end, "label_calendar.end")
    if last < first:
        raise TemporalValidationError("label_calendar_range_invalid")
    rows: list[TemporalLabelInterval] = []
    cursor = first
    while cursor <= last:
        rows.append(
            TemporalLabelInterval(
                sample_id=f"calendar-day:{cursor.isoformat()}",
                observation_date=cursor.isoformat(),
                label_start=cursor.isoformat(),
                label_end=(cursor + timedelta(days=label_horizon_days)).isoformat(),
            )
        )
        cursor += timedelta(days=1)
    return tuple(rows)


def temporal_validation_source_binding_hash(manifest: Any) -> str:
    """Bind a plan to dataset identity and the non-nested window declaration."""

    walk_forward = getattr(manifest, "walk_forward", None)
    if walk_forward is None:
        raise TemporalValidationError("walk_forward_missing")
    dataset = getattr(manifest, "dataset", None)
    if dataset is None or not callable(getattr(dataset, "as_dict", None)):
        raise TemporalValidationError("temporal_validation_dataset_binding_missing")
    return sha256_prefixed(
        {
            "dataset": dataset.as_dict(),
            "research_classification": str(
                getattr(manifest, "research_classification", "")
            ),
            "walk_forward": {
                "train_window_days": int(walk_forward.train_window_days),
                "test_window_days": int(walk_forward.test_window_days),
                "step_days": int(walk_forward.step_days),
                "min_windows": int(walk_forward.min_windows),
            },
        },
        label="nested_temporal_validation_source_binding",
    )


def build_manifest_nested_temporal_validation_plan(
    manifest: Any,
) -> NestedTemporalValidationPlan | None:
    """Public manifest adapter used by readiness and execution admission."""

    walk_forward = getattr(manifest, "walk_forward", None)
    config = getattr(walk_forward, "temporal_validation", None)
    if config is None:
        return None
    from .data_plane import rolling_walk_forward_windows

    raw_windows = rolling_walk_forward_windows(manifest)
    windows = [
        {
            "train": item["train"].as_dict(),
            "test": item["test"].as_dict(),
        }
        for item in raw_windows
    ]
    return build_nested_temporal_validation_plan(
        windows=windows,
        source_binding_hash=temporal_validation_source_binding_hash(manifest),
        config=config,
    )


def parse_nested_temporal_validation_plan(
    value: Mapping[str, Any],
) -> NestedTemporalValidationPlan:
    """Rehydrate persisted evidence and reject any hash or structure drift."""

    if not isinstance(value, Mapping):
        raise TemporalValidationError("temporal_validation_plan_must_be_object")
    expected = {
        "schema_version",
        "source_binding_hash",
        "config",
        "config_hash",
        "label_intervals",
        "label_intervals_hash",
        "outer_folds",
        "content_hash",
    }
    _require_exact_fields(value, expected, "temporal_validation_plan")
    config_value = value["config"]
    if not isinstance(config_value, Mapping):
        raise TemporalValidationError("temporal_validation_config_must_be_object")
    config = parse_nested_temporal_validation_config(config_value)
    if value["config_hash"] != config.contract_hash():
        raise TemporalValidationError("temporal_validation_config_hash_mismatch")
    label_values = value["label_intervals"]
    if not isinstance(label_values, list):
        raise TemporalValidationError("temporal_label_intervals_must_be_array")
    labels = tuple(_parse_label_interval(item) for item in label_values)
    fold_values = value["outer_folds"]
    if not isinstance(fold_values, list):
        raise TemporalValidationError("nested_temporal_outer_folds_must_be_array")
    folds = tuple(_parse_fold(item) for item in fold_values)
    plan = NestedTemporalValidationPlan(
        schema_version=_required_int(value, "schema_version"),
        source_binding_hash=str(value["source_binding_hash"]),
        config=config,
        label_intervals=labels,
        outer_folds=folds,
    )
    if value["label_intervals_hash"] != plan.label_intervals_hash:
        raise TemporalValidationError("temporal_label_intervals_hash_mismatch")
    if value["content_hash"] != plan.contract_hash():
        raise TemporalValidationError("temporal_validation_plan_hash_mismatch")
    return plan


def _build_inner_splits(
    *,
    outer: PurgedTemporalSplit,
    config: NestedTemporalValidationConfig,
    outer_index: int,
) -> tuple[PurgedTemporalSplit, ...]:
    validation_days = config.inner_fold_count * config.inner_test_window_days
    first_test_start = outer.train.end_date - timedelta(days=validation_days - 1)
    rows: list[PurgedTemporalSplit] = []
    for inner_index in range(1, config.inner_fold_count + 1):
        test_start = first_test_start + timedelta(
            days=(inner_index - 1) * config.inner_test_window_days
        )
        test_end = test_start + timedelta(days=config.inner_test_window_days - 1)
        rows.append(
            _build_purged_split(
                split_id=f"inner_{outer_index:03d}_{inner_index:03d}",
                raw_train_start=outer.train.start_date,
                test_start=test_start,
                test_end=test_end,
                config=config,
            )
        )
    return tuple(rows)


def _build_purged_split(
    *,
    split_id: str,
    raw_train_start: date,
    test_start: date,
    test_end: date,
    config: NestedTemporalValidationConfig,
) -> PurgedTemporalSplit:
    embargo_end = test_start - timedelta(days=1)
    embargo_start = embargo_end - timedelta(days=config.embargo_days - 1)
    purge_end = embargo_start - timedelta(days=1)
    purge_start = purge_end - timedelta(days=config.purge_days - 1)
    train_end = purge_start - timedelta(days=1)
    if train_end < raw_train_start:
        raise TemporalValidationError("temporal_split_train_empty_after_exclusion")
    return PurgedTemporalSplit(
        split_id=split_id,
        train=TemporalDateRange(raw_train_start.isoformat(), train_end.isoformat()),
        purge=TemporalDateRange(purge_start.isoformat(), purge_end.isoformat()),
        embargo=TemporalDateRange(embargo_start.isoformat(), embargo_end.isoformat()),
        test=TemporalDateRange(test_start.isoformat(), test_end.isoformat()),
    )


def _parse_source_window(
    value: Mapping[str, Any],
) -> tuple[TemporalDateRange, TemporalDateRange]:
    if not isinstance(value, Mapping):
        raise TemporalValidationError("nested_temporal_source_window_invalid")
    _require_exact_fields(value, {"train", "test"}, "source_window")
    return _parse_range(value["train"]), _parse_range(value["test"])


def _parse_range(value: Any) -> TemporalDateRange:
    if not isinstance(value, Mapping):
        raise TemporalValidationError("temporal_range_must_be_object")
    _require_exact_fields(value, {"start", "end"}, "temporal_range")
    return TemporalDateRange(start=str(value["start"]), end=str(value["end"]))


def _parse_label_interval(value: Any) -> TemporalLabelInterval:
    if not isinstance(value, Mapping):
        raise TemporalValidationError("temporal_label_interval_must_be_object")
    expected = {"sample_id", "observation_date", "label_start", "label_end"}
    _require_exact_fields(value, expected, "temporal_label_interval")
    return TemporalLabelInterval(
        sample_id=str(value["sample_id"]),
        observation_date=str(value["observation_date"]),
        label_start=str(value["label_start"]),
        label_end=str(value["label_end"]),
    )


def _parse_split(value: Any) -> PurgedTemporalSplit:
    if not isinstance(value, Mapping):
        raise TemporalValidationError("purged_temporal_split_must_be_object")
    expected = {"split_id", "train", "purge", "embargo", "test"}
    _require_exact_fields(value, expected, "purged_temporal_split")
    return PurgedTemporalSplit(
        split_id=str(value["split_id"]),
        train=_parse_range(value["train"]),
        purge=_parse_range(value["purge"]),
        embargo=_parse_range(value["embargo"]),
        test=_parse_range(value["test"]),
    )


def _parse_fold(value: Any) -> NestedTemporalFold:
    if not isinstance(value, Mapping):
        raise TemporalValidationError("nested_temporal_fold_must_be_object")
    _require_exact_fields(value, {"outer_split", "inner_splits"}, "temporal_fold")
    inner_values = value["inner_splits"]
    if not isinstance(inner_values, list):
        raise TemporalValidationError("nested_temporal_inner_splits_must_be_array")
    return NestedTemporalFold(
        outer_split=_parse_split(value["outer_split"]),
        inner_splits=tuple(_parse_split(item) for item in inner_values),
    )


def _parse_date(value: str, field: str) -> date:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise TemporalValidationError(f"{field}_must_be_iso_date") from exc
    if parsed.isoformat() != str(value):
        raise TemporalValidationError(f"{field}_must_be_iso_date")
    return parsed


def _require_hash(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise TemporalValidationError(f"temporal_validation_{field}_invalid")


def _require_id(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise TemporalValidationError(f"{field}_invalid")


def _required_int(value: Mapping[str, Any], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int):
        raise TemporalValidationError(f"temporal_validation_{field}_must_be_integer")
    return item


def _require_int_at_least(value: Any, minimum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TemporalValidationError(
            f"temporal_validation_{field}_must_be_at_least_{minimum}"
        )


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise TemporalValidationError(f"{label}_missing:{','.join(missing)}")
    if unknown:
        raise TemporalValidationError(f"{label}_unknown:{','.join(unknown)}")


__all__ = [
    "TEMPORAL_VALIDATION_SCHEMA_VERSION",
    "NestedTemporalValidationConfig",
    "NestedTemporalValidationPlan",
    "NestedTemporalFold",
    "PurgedTemporalSplit",
    "TemporalDateRange",
    "TemporalLabelInterval",
    "TemporalValidationError",
    "build_calendar_label_intervals",
    "build_manifest_nested_temporal_validation_plan",
    "build_nested_temporal_validation_plan",
    "parse_nested_temporal_validation_config",
    "parse_nested_temporal_validation_plan",
    "temporal_validation_source_binding_hash",
]
