from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.cross_section_validation import (
    CROSS_SECTION_VALIDATION_SCHEMA_VERSION,
    CrossSectionSliceIdentity,
    CrossSectionStatus,
    CrossSectionValidationError,
    CrossSectionValidationSpec,
    ValidationSliceEvidence,
    cross_section_report_artifact_path,
    evaluate_cross_section_validation,
    publish_cross_section_dataset_artifact,
    publish_cross_section_result,
    publish_cross_section_spec,
    publish_cross_section_validation_report_artifact,
    query_cross_section_results,
)
from market_research.settings import ResearchSettings


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _identity(
    manager: ResearchPathManager,
    *,
    slice_id: str,
    market: str,
    instrument_id: str,
    marker: str,
) -> CrossSectionSliceIdentity:
    return publish_cross_section_dataset_artifact(
        manager=manager,
        slice_id=slice_id,
        market=market,
        instrument_id=instrument_id,
        asset_class="spot" if market != "US" else "equity",
        dataset_snapshot_hash=_hash(marker),
        period_start="2025-01-01T00:00:00+00:00",
        period_end="2025-12-31T00:00:00+00:00",
    )


def _identities(manager: ResearchPathManager) -> tuple[CrossSectionSliceIdentity, ...]:
    return (
        _identity(
            manager,
            slice_id="slice-btc",
            market="KR",
            instrument_id="inst-btc",
            marker="c",
        ),
        _identity(
            manager,
            slice_id="slice-eth",
            market="KR",
            instrument_id="inst-eth",
            marker="d",
        ),
        _identity(
            manager,
            slice_id="slice-spy",
            market="US",
            instrument_id="inst-spy",
            marker="e",
        ),
    )


def _spec(
    manager: ResearchPathManager,
    *,
    expected_slices: tuple[CrossSectionSliceIdentity, ...] | None = None,
) -> CrossSectionValidationSpec:
    return CrossSectionValidationSpec(
        schema_version=CROSS_SECTION_VALIDATION_SCHEMA_VERSION,
        validation_id="cross-market-001",
        version="1",
        hypothesis_hash=_hash("a"),
        validated_rule_set_hash=_hash("b"),
        expected_slices=expected_slices or _identities(manager),
        minimum_markets=2,
        minimum_instruments=2,
        minimum_total_trades=30,
        minimum_positive_slice_fraction=2 / 3,
        maximum_absolute_return_concentration=0.7,
        frozen_at="2026-01-01T00:00:00+00:00",
        frozen_by="reviewer-a",
    )


def _slice(
    manager: ResearchPathManager,
    expected: CrossSectionSliceIdentity,
    net_return_pct: float,
    *,
    trade_count: int = 20,
) -> ValidationSliceEvidence:
    return publish_cross_section_validation_report_artifact(
        manager=manager,
        expected_slice=expected,
        report_evaluated_at="2026-01-01T12:00:00+00:00",
        status="COMPLETED",
        trade_count=trade_count,
        net_return_pct=net_return_pct,
        expectancy_per_trade_pct=(net_return_pct / trade_count if trade_count else 0.0),
        max_drawdown_pct=5.0 if trade_count else 0.0,
        profit_factor=1.2 if trade_count else 0.0,
    )


def _slices(
    manager: ResearchPathManager,
    identities: tuple[CrossSectionSliceIdentity, ...],
    *,
    returns: tuple[float, ...] | None = None,
) -> tuple[ValidationSliceEvidence, ...]:
    selected_returns = returns or (4.0, 3.0, 2.0)[: len(identities)]
    return tuple(
        _slice(manager, expected, net_return)
        for expected, net_return in zip(identities, selected_returns, strict=True)
    )


def _evaluate(
    manager: ResearchPathManager,
    spec: CrossSectionValidationSpec,
    slices: tuple[ValidationSliceEvidence, ...],
    *,
    evaluated_at: str = "2026-01-02T00:00:00+00:00",
):
    return evaluate_cross_section_validation(
        manager=manager,
        spec=spec,
        slices=slices,
        evaluated_at=evaluated_at,
        evaluated_by="reviewer-b",
    )


def test_cross_market_validation_passes_and_is_searchable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    spec = _spec(manager, expected_slices=identities)
    result = _evaluate(manager, spec, _slices(manager, identities))

    assert result.status is CrossSectionStatus.PASS
    assert result.market_count == 2
    assert result.instrument_count == 3
    assert result.absolute_return_concentration == pytest.approx(4 / 9)
    first = publish_cross_section_spec(manager=manager, spec=spec)
    assert publish_cross_section_spec(manager=manager, spec=spec) == first
    published = publish_cross_section_result(manager=manager, result=result)
    assert publish_cross_section_result(manager=manager, result=result) == published
    assert query_cross_section_results(manager=manager, status="PASS", market="US")
    assert query_cross_section_results(manager=manager, instrument_id="inst-spy")


def test_predeclared_full_slice_identity_cannot_be_changed_or_omitted(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    slices = _slices(manager, identities)
    spec = _spec(manager, expected_slices=identities)
    with pytest.raises(CrossSectionValidationError, match="slice_set_mismatch"):
        _evaluate(manager, spec, slices[:-1])

    changed_identity = replace(identities[0], market="fabricated-market")
    changed_spec = _spec(
        manager,
        expected_slices=(changed_identity, *identities[1:]),
    )
    with pytest.raises(CrossSectionValidationError, match="slice_identity_mismatch"):
        _evaluate(manager, changed_spec, slices)


def test_failed_slice_is_preserved_and_makes_result_inconclusive(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    failed = publish_cross_section_validation_report_artifact(
        manager=manager,
        expected_slice=identities[2],
        report_evaluated_at="2026-01-01T12:00:00+00:00",
        status="FAILED",
        trade_count=None,
        net_return_pct=None,
        expectancy_per_trade_pct=None,
        max_drawdown_pct=None,
        profit_factor=None,
        failure_code="dataset_quality_failed",
    )
    completed = _slices(manager, identities[:2])
    result = _evaluate(
        manager,
        _spec(manager, expected_slices=identities),
        (*completed, failed),
    )

    assert result.status is CrossSectionStatus.INCONCLUSIVE
    assert result.failed_slice_count == 1
    assert "cross_section_failed_slices_present" in result.reasons


def test_concentrated_or_broadly_negative_results_fail(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    result = _evaluate(
        manager,
        _spec(manager, expected_slices=identities),
        _slices(manager, identities, returns=(100.0, -1.0, -1.0)),
    )

    assert result.status is CrossSectionStatus.FAIL
    assert "cross_section_return_concentration_failed" in result.reasons
    assert "cross_section_positive_slice_fraction_failed" in result.reasons


def test_zero_trade_slice_cannot_contribute_as_positive(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    zero = _slice(manager, identities[0], 0.0, trade_count=0)
    rest = _slices(manager, identities[1:])
    result = _evaluate(
        manager,
        _spec(manager, expected_slices=identities),
        (zero, *rest),
    )
    assert result.status is CrossSectionStatus.INCONCLUSIVE
    assert result.positive_slice_fraction == pytest.approx(2 / 3)
    assert "cross_section_zero_trade_slices_present" in result.reasons

    with pytest.raises(CrossSectionValidationError, match="zero_trade_metrics_nonzero"):
        _slice(manager, identities[0], 1.0, trade_count=0)


def test_evaluation_must_follow_freeze_period_and_terminal_reports(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    with pytest.raises(CrossSectionValidationError, match="not_after_evidence"):
        _evaluate(
            manager,
            _spec(manager, expected_slices=identities),
            _slices(manager, identities),
            evaluated_at="2026-01-01T06:00:00+00:00",
        )


def test_result_fields_and_content_hash_cannot_be_forged(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    result = _evaluate(
        manager,
        _spec(manager, expected_slices=identities),
        _slices(manager, identities),
    )

    with pytest.raises(CrossSectionValidationError, match="semantic_mismatch"):
        replace(result, status=CrossSectionStatus.FAIL)
    with pytest.raises(CrossSectionValidationError, match="content_hash_mismatch"):
        replace(result, content_hash=_hash("f"))


def test_missing_or_tampered_authority_targets_fail_closed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    slices = _slices(manager, identities)
    spec = _spec(manager, expected_slices=identities)
    result = _evaluate(manager, spec, slices)
    publish_cross_section_spec(manager=manager, spec=spec)
    publish_cross_section_result(manager=manager, result=result)

    report_path = cross_section_report_artifact_path(
        manager, slices[0].validation_report_hash
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["trade_count"] = 999
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CrossSectionValidationError, match="artifact_hash_mismatch"):
        query_cross_section_results(manager=manager)

    report_path.unlink()
    with pytest.raises(CrossSectionValidationError, match="artifact_unreadable"):
        query_cross_section_results(manager=manager)


def test_same_version_conflict_and_unpublished_spec_fail_closed(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    identities = _identities(manager)
    spec = _spec(manager, expected_slices=identities)
    result = _evaluate(manager, spec, _slices(manager, identities))
    publish_cross_section_spec(manager=manager, spec=spec)
    with pytest.raises(CrossSectionValidationError, match="event_id_conflict"):
        publish_cross_section_spec(
            manager=manager,
            spec=replace(spec, minimum_total_trades=31),
        )
    other_manager = _manager(tmp_path / "other")
    with pytest.raises(CrossSectionValidationError, match="artifact_unreadable"):
        publish_cross_section_result(manager=other_manager, result=result)
