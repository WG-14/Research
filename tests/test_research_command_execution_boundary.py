from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


FORBIDDEN_MODULES = (
    "bithumb_bot.config",
    "bithumb_bot.broker",
    "bithumb_bot.approved_profile",
    "bithumb_bot.notifier",
    "bithumb_bot.notification_outbox",
    "bithumb_bot.runtime_strategy_decision",
    "bithumb_bot.runtime_strategy_set",
    "bithumb_bot.recovery",
)


def _run(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_workload_estimate_executes_without_operational_modules() -> None:
    script = """
import sys
from bithumb_bot.research_cli.main import main
assert main(['research-workload-estimate', '--manifest', 'examples/research/sma_filter_manifest.example.json', '--json']) == 0
for name in {forbidden!r}:
    assert name not in sys.modules, name
""".format(forbidden=FORBIDDEN_MODULES)

    result = _run(script)

    assert result.returncode == 0, result.stderr
    assert '"work_unit_count"' in result.stdout


def test_readiness_uses_research_db_without_operational_modules(tmp_path: Path) -> None:
    db_path = tmp_path / "research.sqlite"
    script = """
import sqlite3
import sys
from pathlib import Path
from bithumb_bot.research_cli.main import main
db = Path({db_path!r})
with sqlite3.connect(db) as conn:
    conn.execute('CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)')
assert main(['research-readiness', '--manifest', 'examples/research/sma_filter_manifest.example.json', '--json']) == 1
for name in {forbidden!r}:
    assert name not in sys.modules, name
""".format(db_path=str(db_path), forbidden=FORBIDDEN_MODULES)
    env = os.environ.copy()
    env.update({
        "RESEARCH_DATA_ROOT": str(tmp_path / "datasets"),
        "RESEARCH_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "RESEARCH_REPORT_ROOT": str(tmp_path / "reports"),
        "RESEARCH_CACHE_ROOT": str(tmp_path / "cache"),
        "RESEARCH_DB_PATH": str(db_path),
    })

    result = _run(script, env=env)

    assert result.returncode == 0, result.stderr
    assert '"settings_source": "RESEARCH_*"' in result.stdout


def test_backtest_failure_path_uses_research_context_without_operational_modules(tmp_path: Path) -> None:
    db_path = tmp_path / "research.sqlite"
    script = """
import sqlite3
import sys
from pathlib import Path
from bithumb_bot.research_cli.main import main
db = Path({db_path!r})
with sqlite3.connect(db) as conn:
    conn.execute('CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)')
assert main(['research-backtest', '--manifest', 'examples/research/sma_filter_manifest.example.json', '--notification-policy', 'disabled']) == 1
for name in {forbidden!r}:
    assert name not in sys.modules, name
""".format(db_path=str(db_path), forbidden=FORBIDDEN_MODULES)
    env = os.environ.copy()
    env.update({
        "RESEARCH_DATA_ROOT": str(tmp_path / "datasets"),
        "RESEARCH_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "RESEARCH_REPORT_ROOT": str(tmp_path / "reports"),
        "RESEARCH_CACHE_ROOT": str(tmp_path / "cache"),
        "RESEARCH_DB_PATH": str(db_path),
    })

    result = _run(script, env=env)

    assert result.returncode == 0, result.stderr
    assert "[RESEARCH-BACKTEST] error=dataset split train has no candles" in result.stdout


def test_research_context_transition_left_no_global_replacement_residue() -> None:
    root = Path("src/bithumb_bot")
    text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "research").glob("*.py"))
    text += "\n" + "\n".join(path.read_text(encoding="utf-8") for path in (root / "research_cli").glob("*.py"))

    assert not (root / "research" / "legacy_config.py").exists()
    for forbidden in ("legacy.PATH_MANAGER =", "legacy.settings =", "LazyOperationalConfigValue"):
        assert forbidden not in text
