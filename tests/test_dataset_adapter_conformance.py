from __future__ import annotations

from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE
from pathlib import Path

from market_research.research.datasets.verification import DatasetVerificationResult, VerificationStatus, verification_allowed
from market_research.research.datasets.registry import DatasetAdapterRegistry, default_dataset_adapter_registry
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.dataset_snapshot import Candle, DatasetLoadContext, DatasetSnapshot, FrozenSQLiteCandleAdapter, SQLiteCandleAdapter
from market_research.research.datasets.contracts import DatasetArtifactRef, DatasetResolutionContext, DatasetSliceQuery
from market_research.research.experiment_manifest import DateRange
from market_research.research_composition import load_builtin_manifest as load_manifest
from market_research.research.validation_protocol import run_research_backtest
from market_research.research_composition import builtin_strategy_registry
from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings
import pytest

from .research_noop_success_fixture import create_success_fixture
from .test_dataset_artifact_manifest_contract import _source


def _frozen_fixture(tmp_path: Path) -> tuple[object, DatasetVerificationResult]:
    frozen = freeze_sqlite_candles_dataset(source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2,
        out_dir=tmp_path / "frozen-out",
    )
    adapter = FrozenSQLiteCandleAdapter()
    handle = adapter.resolve(
        DatasetArtifactRef(frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"]),
        DatasetResolutionContext(),
    )
    verified = adapter.verify(handle)
    snapshot = adapter.materialize(verified, query=DatasetSliceQuery("KRW-BTC", "1m", 1, 2, "train", "frozen", {}))
    return snapshot, adapter.verify_snapshot(snapshot=snapshot, context=DatasetLoadContext())


def _sqlite_fixture(_: Path) -> tuple[object, DatasetVerificationResult]:
    snapshot = DatasetSnapshot(
        "mutable", "sqlite_candles", "KRW-BTC", "1m", "train", DateRange("2026-01-01", "2026-01-01"),
        (Candle(1767225600000, 1, 1, 1, 1, 1),), source_content_hash="sha256:" + "a" * 64,
    )
    return snapshot, SQLiteCandleAdapter().verify_snapshot(snapshot=snapshot, context=DatasetLoadContext())


CONFORMANCE_FIXTURE_FACTORIES = {
    "frozen_sqlite_candles": _frozen_fixture,
    "sqlite_candles": _sqlite_fixture,
}


def test_conformance_fixture_exists_for_every_registered_adapter() -> None:
    assert set(default_dataset_adapter_registry().sources()) == set(CONFORMANCE_FIXTURE_FACTORIES)


@pytest.mark.parametrize("source", default_dataset_adapter_registry().sources())
def test_all_registered_adapters_pass_verification_contract(tmp_path: Path, source: str) -> None:
    snapshot, result = CONFORMANCE_FIXTURE_FACTORIES[source](tmp_path)
    assert isinstance(result, DatasetVerificationResult)
    assert snapshot.source == source
    if source == "frozen_sqlite_candles":
        assert result.overall_status is VerificationStatus.VERIFIED
        assert result.content_status is VerificationStatus.VERIFIED
        assert result.schema_status is VerificationStatus.VERIFIED
        assert result.locator_status is VerificationStatus.VERIFIED
        assert result.scope_status is VerificationStatus.VERIFIED
        assert result.actual_content_hash and result.actual_schema_hash
    else:
        assert result.overall_status in {VerificationStatus.DECLARED_ONLY, VerificationStatus.DERIVED_FROM_SNAPSHOT}
        assert result.overall_status is not VerificationStatus.VERIFIED
        assert result.actual_content_hash is None


def test_registry_rejects_adapter_without_verification_capability() -> None:
    class IncompleteAdapter:
        source = "incomplete"
        requires_runtime_db = False
        requires_artifact_manifest = False
        load_range = quality_report = provenance = lambda self, **_: None
    with pytest.raises(ValueError, match="verify_snapshot"):
        DatasetAdapterRegistry().register(IncompleteAdapter())


def test_sqlite_adapter_does_not_claim_verified_without_full_scan(tmp_path: Path) -> None:
    _, result = _sqlite_fixture(tmp_path)
    assert result.overall_status is VerificationStatus.DECLARED_ONLY
    assert result.actual_content_hash is None
    assert result.expected_content_hash is None


def test_validated_candidate_rejects_declared_only_sqlite(tmp_path: Path) -> None:
    _, result = _sqlite_fixture(tmp_path)
    assert not verification_allowed(classification="validated_candidate", result=result)


def test_mismatch_fails_before_strategy_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, manifest_path = create_success_fixture(tmp_path)
    manager = ResearchPathManager.from_settings(ResearchSettings(
        data_root=tmp_path / "data", artifact_root=tmp_path / "artifacts", report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache", db_path=db_path, max_workers=1, random_seed=0,
    ), project_root=Path.cwd())
    calls = 0
    def mismatch(self, *, snapshot, context):
        del self, snapshot, context
        return DatasetVerificationResult(
            VerificationStatus.MISMATCH, VerificationStatus.MISMATCH, "sha256:" + "a" * 64,
            "sha256:" + "b" * 64, "complete_scan", VerificationStatus.VERIFIED,
            "sha256:" + "c" * 64, "sha256:" + "c" * 64, VerificationStatus.VERIFIED,
            "local", VerificationStatus.VERIFIED, {}, {}, "sqlite_candle_adapter", "1",
        )

    class SpyEvaluator:
        def evaluate(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("strategy runner must not execute after verification mismatch")

    monkeypatch.setattr(SQLiteCandleAdapter, "verify_snapshot", mismatch)
    with pytest.raises(Exception, match="dataset_verification_mismatch_before_strategy_execution"):
        run_research_backtest(
            manifest=load_manifest(manifest_path), db_path=db_path, manager=manager,
            strategy_registry=builtin_strategy_registry(),
            candidate_evaluator=SpyEvaluator(),
        )
    assert calls == 0


def test_verification_policy_is_status_based_and_fail_closed() -> None:
    result = DatasetVerificationResult(VerificationStatus.MISMATCH, VerificationStatus.MISMATCH, "sha256:"+"a"*64, "sha256:"+"b"*64, "scan", VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, VerificationStatus.VERIFIED, "content_addressed_local", VerificationStatus.VERIFIED, {}, {}, "adapter", "1")
    assert not verification_allowed(classification="research_only", result=result)
    assert not verification_allowed(classification="validated_candidate", result=result)


def test_verified_requires_all_verified_components() -> None:
    with pytest.raises(ValueError, match="components"):
        DatasetVerificationResult(VerificationStatus.VERIFIED, VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, "scan", VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, VerificationStatus.UNAVAILABLE, "local", VerificationStatus.VERIFIED, {}, {}, "adapter", "1")


def test_any_component_mismatch_requires_overall_mismatch() -> None:
    with pytest.raises(ValueError, match="component_mismatch"):
        DatasetVerificationResult(VerificationStatus.DECLARED_ONLY, VerificationStatus.MISMATCH, "sha256:"+"a"*64, "sha256:"+"b"*64, "scan", VerificationStatus.DECLARED_ONLY, None, None, VerificationStatus.UNAVAILABLE, None, VerificationStatus.DERIVED_FROM_SNAPSHOT, None, None, "adapter", "1")


@pytest.mark.parametrize(
    ("status", "expected", "actual", "message"),
    (
        (VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"b"*64, "verified_hash_mismatch"),
        (VerificationStatus.MISMATCH, "sha256:"+"a"*64, "sha256:"+"a"*64, "mismatch_hash_equal"),
    ),
)
def test_verification_hash_relationships_are_constructor_enforced(status, expected, actual, message) -> None:
    with pytest.raises(ValueError, match=message):
        DatasetVerificationResult(VerificationStatus.MISMATCH, status, expected, actual, "scan", VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, VerificationStatus.VERIFIED, "local", VerificationStatus.VERIFIED, {}, {}, "adapter", "1")
