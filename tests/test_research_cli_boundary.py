from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from market_research.paths import ResearchPathManager
from market_research.research_cli.registry import command_registry
from market_research.settings import ResearchSettings


RESEARCH_COMMANDS = {
    "research-backtest",
    "research-walk-forward",
    "research-validate",
    "research-readiness",
    "research-freeze-dataset",
    "research-workload-estimate",
    "research-batch",
    "research-forward-diagnostics",
    "research-verify-audit",
    "research-reproduce-run",
    "research-registry-inspect",
    "research-registry-validate",
    "research-mark-attempt-aborted",
}

FORBIDDEN_OPERATIONAL_COMMANDS = {
    "run",
    "health",
    "sync",
    "ticker",
    "status",
    "trades",
    "ops-report",
    "live-dry-run",
    "runtime-strategy-set-lint",
    "runtime-strategy-set-dump",
    "runtime-replay-decisions",
    "replay-decision",
    "profile-generate",
    "profile-verify",
    "profile-diff",
    "decision-equivalence",
}

FORBIDDEN_MODULES = {
    "market_research." + "config",
    "market_research." + "broker",
    "market_research.research_profile",
    "market_research.runtime_strategy_decision",
    "market_research.runtime_strategy_set",
    "market_research.recovery",
}


def test_research_registry_only_contains_research_commands() -> None:
    registry = command_registry()

    assert set(registry) == RESEARCH_COMMANDS
    assert not (set(registry) & FORBIDDEN_OPERATIONAL_COMMANDS)


def test_research_settings_default_to_external_roots_without_creating_outputs(monkeypatch, tmp_path) -> None:
    for key in (
        "RESEARCH_DATA_ROOT",
        "RESEARCH_ARTIFACT_ROOT",
        "RESEARCH_REPORT_ROOT",
        "RESEARCH_CACHE_ROOT",
        "RESEARCH_DB_PATH",
        "RESEARCH_MAX_WORKERS",
        "RESEARCH_RANDOM_SEED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    settings = ResearchSettings.from_env()
    paths = ResearchPathManager.from_settings(settings, project_root=Path.cwd())

    assert settings.db_path is None
    assert paths.data_root == tmp_path / "state" / "market-research" / "datasets"
    assert paths.artifact_path("derived", "candidate.json") == settings.artifact_root / "derived" / "candidate.json"
    assert paths.report_path("research", "summary.json") == settings.report_root / "research" / "summary.json"
    assert not settings.data_root.exists()


def test_research_help_has_no_operational_import_or_environment_requirement() -> None:
    script = """
import sys
from market_research.research_cli.main import main
try:
    main(['--help'])
except SystemExit as exc:
    assert exc.code == 0
for name in {
    'market_research.' + 'config',
    'market_research.' + 'broker',
    'market_research.research_profile',
    'market_research.runtime_strategy_decision',
    'market_research.runtime_strategy_set',
    'market_research.recovery',
}:
    assert name not in sys.modules, name
"""
    env = os.environ.copy()
    for key in (
        "MODE",
        "RESEARCH_API_KEY",
        "RESEARCH_API_SECRET",
        "APPROVED_STRATEGY_PROFILE_PATH",
        "LIVE_DRY_RUN",
        "LIVE_REAL_ORDER_ARMED",
    ):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "research-backtest" in result.stdout
    assert "live-dry-run" not in result.stdout
    assert "recovery-report" not in result.stdout
