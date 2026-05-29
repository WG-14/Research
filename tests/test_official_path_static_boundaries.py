from __future__ import annotations

from pathlib import Path
import re
import ast


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
    "build_execution_decision_summary(",
)

EXPLICIT_COMPATIBILITY_PATHS = {
    "src/bithumb_bot/strategy/registry.py",
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


def test_legacy_sma_db_bound_implementation_is_owned_by_compat_namespace() -> None:
    compat_source = (REPO / "src/bithumb_bot/compat/sma_legacy_adapter.py").read_text(encoding="utf-8")
    strategy_shim_source = (REPO / "src/bithumb_bot/strategy/sma_legacy_adapter.py").read_text(
        encoding="utf-8"
    )
    strategy_sma_source = (REPO / "src/bithumb_bot/strategy/sma.py").read_text(encoding="utf-8")

    for marker in (
        "class SmaCrossStrategy",
        "class LegacySmaWithFilterDbAdapter",
        "def create_sma_strategy",
        "def create_legacy_sma_with_filter_db_adapter",
        "LEGACY_DB_BOUND_STRATEGY_STATUS",
    ):
        assert marker in compat_source
    for marker in (
        "class SmaCrossStrategy",
        "class LegacySmaWithFilterDbAdapter",
        "def create_sma_strategy",
        "def create_legacy_sma_with_filter_db_adapter",
    ):
        assert marker not in strategy_shim_source
        assert marker not in strategy_sma_source
    assert "from bithumb_bot.compat.sma_legacy_adapter import" in strategy_shim_source
    assert "__all__" in strategy_sma_source
    assert "SmaCrossStrategy" not in strategy_sma_source
    assert "LegacySmaWithFilterDbAdapter" not in strategy_sma_source


def test_runtime_source_does_not_import_strategy_sma_legacy_adapter() -> None:
    violations: list[str] = []
    for path in (REPO / "src/bithumb_bot").rglob("*.py"):
        relative = path.relative_to(REPO).as_posix()
        if relative == "src/bithumb_bot/strategy/sma_legacy_adapter.py":
            continue
        source = path.read_text(encoding="utf-8-sig")
        if "bithumb_bot.strategy.sma_legacy_adapter" in source:
            violations.append(relative)
        if "from .sma_legacy_adapter" in source or "from ..strategy.sma_legacy_adapter" in source:
            violations.append(relative)

    assert violations == []


def test_promotion_runtime_adapters_use_strategy_decision_service_boundary() -> None:
    adapter_paths = (
        REPO / "src/bithumb_bot/runtime_adapters/sma_with_filter.py",
        REPO / "src/bithumb_bot/runtime_adapters/safe_hold.py",
        REPO / "src/bithumb_bot/strategy_plugins/canary_non_sma.py",
    )
    failures: list[str] = []
    for path in adapter_paths:
        source = path.read_text(encoding="utf-8-sig")
        if path.name == "sma_with_filter.py" and "compute_strategy_decision_after_normalization" in source:
            continue
        if "StrategyDecisionService" not in source or "StrategyEvaluationRequest" not in source:
            failures.append(path.relative_to(REPO).as_posix())
    assert failures == []


def test_promotion_runtime_adapters_do_not_construct_strategy_decision_v2_outside_policy_classes() -> None:
    allowed_class_suffix = ("Policy", "Strategy")
    failures: list[str] = []
    for path in (REPO / "src/bithumb_bot").rglob("*.py"):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith("src/bithumb_bot/compat/") or "/tests/" in relative:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name != "StrategyDecisionV2":
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(owner, ast.ClassDef):
                owner = parents.get(owner)
            if isinstance(owner, ast.ClassDef) and owner.name.endswith(allowed_class_suffix):
                continue
            if relative == "src/bithumb_bot/core/sma_policy.py":
                continue
            failures.append(f"{relative}:{node.lineno}")
    assert failures == []


def test_engine_is_thin_runtime_entrypoint() -> None:
    source = (REPO / "src/bithumb_bot/engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]

    assert len(functions) <= 10
    assert "operator_next_action" not in source
    assert "operator_hint_command" not in source
    assert "cancel_open_orders_with_broker" not in source
    assert "flatten_position" not in source
    assert "LIVE_EXECUTION_BROKER_ERROR" not in source
    assert "STARTUP_SAFETY_GATE" not in source


def test_runtime_recovery_controller_evaluate_phase_has_no_mutation_or_delivery_calls() -> None:
    source = (REPO / "src/bithumb_bot/runtime/recovery_controller.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    evaluate_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_clearance"
    ]
    assert len(evaluate_nodes) == 1
    evaluate_source = ast.get_source_segment(source, evaluate_nodes[0]) or ""
    forbidden = (
        "disable_trading_until",
        "enter_halt",
        "set_resume_gate",
        "notify",
        "send_event",
        "send_message",
        "cancel_open_orders",
        "flatten",
        "get_balance",
    )
    assert all(marker not in evaluate_source for marker in forbidden)


def test_notification_composer_has_no_runtime_state_side_effects() -> None:
    source = (REPO / "src/bithumb_bot/runtime/operator_event_composer.py").read_text(encoding="utf-8")
    assert "runtime_state" not in source
    assert "notify" not in source
    assert "send_message" not in source
    assert "send_event" not in source


def test_runtime_runner_delegates_safety_recovery_and_execution_boundaries() -> None:
    source = (REPO / "src/bithumb_bot/runtime/runner.py").read_text(encoding="utf-8-sig")

    assert "RecoveryController(" in source
    assert "SafetyController(" in source
    assert "StartupController(" in source
    assert "ExecutionCoordinator(" in source
    assert "cancel_open_orders_with_broker(broker)" not in source
    assert "flatten_status = str(flatten_outcome.get" not in source
