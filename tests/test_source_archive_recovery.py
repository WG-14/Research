from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import (
    DateRange,
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.source_archive import (
    SourceArchiveError,
    publish_source_archive,
    restore_source_archive,
)
from market_research.research_composition.builtin_registry import (
    builtin_strategy_registry,
)
from market_research.settings import ResearchSettings


def _manager(tmp_path: Path) -> ResearchPathManager:
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    return ResearchPathManager.from_settings(
        settings, project_root=Path(__file__).resolve().parents[1]
    )


def test_archive_is_content_addressed_and_restores_executable_strategy(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    registry = builtin_strategy_registry()

    first = publish_source_archive(
        manager=manager,
        strategy_name="noop_baseline",
        strategy_registry=registry,
    )
    second = publish_source_archive(
        manager=manager,
        strategy_name="noop_baseline",
        strategy_registry=registry,
    )
    assert first == second
    assert first["sidecar_manifest_digest"]
    assert str(first["strategy_package_digest"]).startswith("sha256:")

    restored = restore_source_archive(
        archive_path=str(first["path"]),
        expected_digest=str(first["digest"]),
        destination=tmp_path / "restored",
    )
    # Calculation recovery uses only the restored source tree, after the
    # currently installed package path is deliberately removed from sys.path.
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(restored / 'src')!r}); "
                "from market_research.research.dataset_snapshot import Candle,DatasetSnapshot; "
                "from market_research.research.experiment_manifest import DateRange,ExecutionTimingPolicy,legacy_research_portfolio_policy; "
                "from market_research.research.simulation_engine import run_common_simulation_backtest; "
                "from market_research.research_composition.builtin_registry import builtin_strategy_registry; "
                "r=builtin_strategy_registry(); p=r.resolve('noop_baseline'); "
                "d=DatasetSnapshot('engine','archive','KRW-BTC','1m','validation',DateRange('2026-01-01','2026-01-01'),tuple(Candle(i*60000,100+i,101+i,99+i,100+i,1.0) for i in range(5))); "
                "x=run_common_simulation_backtest(plugin=p,registry=r,dataset=d,parameter_values={},fee_rate=0.001,slippage_bps=10.0,execution_timing_policy=ExecutionTimingPolicy(fill_reference_policy='next_candle_open',allow_same_candle_close_fill=False),portfolio_policy=legacy_research_portfolio_policy()); "
                "print(p.name,p.contract_hash(),x.decision_stream_hash,x.metrics_hash)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    fields = probe.stdout.strip().split()
    assert len(fields) == 4
    assert fields[0] == "noop_baseline"
    assert all(value.startswith("sha256:") for value in fields[1:])
    plugin = registry.resolve("noop_baseline")
    current = run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        dataset=DatasetSnapshot(
            "engine",
            "archive",
            "KRW-BTC",
            "1m",
            "validation",
            DateRange("2026-01-01", "2026-01-01"),
            tuple(
                Candle(i * 60_000, 100 + i, 101 + i, 99 + i, 100 + i, 1.0)
                for i in range(5)
            ),
        ),
        parameter_values={},
        fee_rate=0.001,
        slippage_bps=10.0,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open",
            allow_same_candle_close_fill=False,
        ),
        portfolio_policy=legacy_research_portfolio_policy(),
    )
    assert fields[1:] == [
        plugin.contract_hash(),
        current.decision_stream_hash,
        current.metrics_hash,
    ]


def test_restore_rejects_tampered_archive(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    evidence = publish_source_archive(
        manager=manager,
        strategy_name="noop_baseline",
        strategy_registry=builtin_strategy_registry(),
    )
    archive = Path(str(evidence["path"]))
    with archive.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(SourceArchiveError, match="source_archive_digest_mismatch"):
        restore_source_archive(
            archive_path=archive,
            expected_digest=str(evidence["digest"]),
            destination=tmp_path / "restore-tampered",
        )
