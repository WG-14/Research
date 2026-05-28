from __future__ import annotations

from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]

OFFICIAL_PATHS = (
    "src/bithumb_bot/approved_profile.py",
    "src/bithumb_bot/config.py",
    "src/bithumb_bot/evidence_chain.py",
    "src/bithumb_bot/engine.py",
    "src/bithumb_bot/execution_service.py",
    "src/bithumb_bot/profile_cli.py",
    "src/bithumb_bot/runtime_adapter_bootstrap.py",
    "src/bithumb_bot/runtime_strategy_decision.py",
    "src/bithumb_bot/runtime_strategy_set.py",
    "src/bithumb_bot/execution_service.py",
    "src/bithumb_bot/run_loop_execution_planner.py",
    "src/bithumb_bot/decision_envelope.py",
    "src/bithumb_bot/decision_equivalence.py",
    "src/bithumb_bot/broker/live.py",
    "src/bithumb_bot/research",
    "src/bithumb_bot/runtime_sma_snapshot.py",
    "src/bithumb_bot/runtime_sma_snapshot_builder.py",
)

FORBIDDEN_MARKERS = (
    "import backtest",
    "from backtest",
    "smoke_backtest",
    "diagnostic_smoke_backtest",
    "SmaCrossStrategy",
    "LegacySmaWithFilterDbAdapter",
    "bithumb_bot.strategy.registry",
    ".strategy.registry",
    "register_strategy(",
    "register_legacy_strategy(",
    "create_legacy_strategy",
    "create_strategy(",
    "register_runtime_decision_adapter(",
    "build_execution_decision_summary(",
)

EXPLICIT_COMPATIBILITY_PATHS = {
    "src/bithumb_bot/strategy/registry.py",
    "src/bithumb_bot/strategy/sma_legacy_adapter.py",
}


def _iter_official_files() -> list[Path]:
    files: list[Path] = []
    for relative in OFFICIAL_PATHS:
        path = REPO / relative
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        else:
            files.append(path)
    return [
        path
        for path in files
        if path.exists()
        and path.relative_to(REPO).as_posix() not in EXPLICIT_COMPATIBILITY_PATHS
    ]


def _marker_allowed(relative: str, line: str, marker: str) -> bool:
    stripped = line.strip()
    if marker == "build_execution_decision_summary(":
        return (
            relative == "src/bithumb_bot/execution_service.py"
            and stripped.startswith("def build_execution_decision_summary(")
        )
    if marker == "create_strategy(":
        return relative.endswith("strategy_registry.py")
    if marker == "register_runtime_decision_adapter(":
        return (
            relative == "src/bithumb_bot/runtime_strategy_decision.py"
            and stripped.startswith("def register_runtime_decision_adapter(")
        )
    return False


def _line_has_marker(line: str, marker: str) -> bool:
    if marker == "import backtest":
        return bool(re.search(r"\bimport\s+backtest\b", line))
    if marker == "from backtest":
        return bool(re.search(r"\bfrom\s+backtest\b", line))
    return marker in line


def test_official_paths_do_not_cross_smoke_or_legacy_authority_boundaries() -> None:
    failures: list[str] = []
    for path in _iter_official_files():
        relative = path.relative_to(REPO).as_posix()
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for marker in FORBIDDEN_MARKERS:
                if _line_has_marker(line, marker) and not _marker_allowed(relative, line, marker):
                    failures.append(f"{relative}:{line_no}: forbidden marker {marker!r}")

    assert failures == []
