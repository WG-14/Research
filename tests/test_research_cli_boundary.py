from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from bithumb_bot.research_cli.paths import ResearchPathManager
from bithumb_bot.research_cli.registry import command_registry
from bithumb_bot.research_cli.settings import ResearchSettings


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
    "research-reproduce",
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
    "research-promote-candidate",
}

FORBIDDEN_MODULES = {
    "bithumb_bot.config",
    "bithumb_bot.broker",
    "bithumb_bot.approved_profile",
    "bithumb_bot.runtime_strategy_decision",
    "bithumb_bot.runtime_strategy_set",
    "bithumb_bot.recovery",
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
        "RESEARCH_NOTIFICATION_POLICY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    settings = ResearchSettings.from_env()
    paths = ResearchPathManager.from_settings(settings, project_root=Path.cwd())

    assert settings.notification_policy == "disabled"
    assert settings.db_path is None
    assert paths.data_root == tmp_path / "state" / "bithumb-research" / "datasets"
    assert paths.artifact_path("derived", "candidate.json") == settings.artifact_root / "derived" / "candidate.json"
    assert paths.report_path("research", "summary.json") == settings.report_root / "research" / "summary.json"
    assert not settings.data_root.exists()


def test_research_help_has_no_operational_import_or_environment_requirement() -> None:
    script = """
import sys
from bithumb_bot.research_cli.main import main
try:
    main(['--help'])
except SystemExit as exc:
    assert exc.code == 0
for name in {
    'bithumb_bot.config',
    'bithumb_bot.broker',
    'bithumb_bot.approved_profile',
    'bithumb_bot.runtime_strategy_decision',
    'bithumb_bot.runtime_strategy_set',
    'bithumb_bot.recovery',
}:
    assert name not in sys.modules, name
"""
    env = os.environ.copy()
    for key in (
        "MODE",
        "BITHUMB_API_KEY",
        "BITHUMB_API_SECRET",
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
