from __future__ import annotations

import socket
import sys
import types
from pathlib import Path

import pytest

import bithumb_bot.config as _config_module
from bithumb_bot.config import settings
from bithumb_bot.paths import PathConfig, PathManager


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
def _path_manager_for_runtime_root(runtime_root: Path) -> PathManager:
    return PathManager(
        project_root=_ROOT.resolve(),
        config=PathConfig(
            mode="paper",
            env_root=runtime_root / "env",
            run_root=runtime_root / "run",
            data_root=runtime_root / "data",
            log_root=runtime_root / "logs",
            backup_root=runtime_root / "backup",
            archive_root=runtime_root / "archive",
        ),
    )


_BASE_RUNTIME_ROOT = Path("/tmp/bithumb-bot-pytest-runtime").resolve()
_BASE_PATH_MANAGER = _path_manager_for_runtime_root(_BASE_RUNTIME_ROOT)
if _SRC.is_dir():
    src_path = str(_SRC)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)


def _sync_config_singletons(path_manager=None) -> None:
    manager = _BASE_PATH_MANAGER if path_manager is None else path_manager
    _config_module.settings = settings
    _config_module.PATH_MANAGER = manager
    for module_name, module in tuple(sys.modules.items()):
        if (
            module_name.startswith("bithumb_bot")
            and getattr(module, "settings", None) is not settings
            and hasattr(module, "settings")
        ):
            setattr(module, "settings", settings)
        if getattr(module, "PATH_MANAGER", None) is not manager and hasattr(module, "PATH_MANAGER"):
            setattr(module, "PATH_MANAGER", manager)


try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    mod = types.ModuleType("httpx")

    class RequestError(Exception):
        pass

    class HTTPStatusError(Exception):
        def __init__(self, message: str, request=None, response=None):
            super().__init__(message)
            self.request = request
            self.response = response

    class Request:
        def __init__(self, method: str, url: str):
            self.method = method
            self.url = url

    class Response:
        def __init__(self, status_code: int, request: Request | None = None, json=None):
            self.status_code = status_code
            self.request = request
            self._json = json

        def raise_for_status(self) -> None:
            if int(self.status_code) >= 400:
                raise HTTPStatusError(
                    f"HTTP {self.status_code}",
                    request=self.request,
                    response=self,
                )

        def json(self):
            return self._json

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    mod.RequestError = RequestError
    mod.HTTPStatusError = HTTPStatusError
    mod.Request = Request
    mod.Response = Response
    mod.Client = Client

    sys.modules["httpx"] = mod


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    def _deny(*args, **kwargs):
        raise RuntimeError("external network is disabled in tests")

    monkeypatch.setattr(socket, "create_connection", _deny)


@pytest.fixture
def managed_runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Inject managed runtime roots/DB under pytest tmp_path (never repo-local)."""
    project_root = _ROOT.resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    assert project_root not in runtime_root.parents

    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("ENV_ROOT", str(runtime_root / "env"))
    monkeypatch.setenv("RUN_ROOT", str(runtime_root / "run"))
    monkeypatch.setenv("DATA_ROOT", str(runtime_root / "data"))
    monkeypatch.setenv("LOG_ROOT", str(runtime_root / "logs"))
    monkeypatch.setenv("BACKUP_ROOT", str(runtime_root / "backup"))

    manager = PathManager.from_env(project_root=project_root)
    db_path = manager.primary_db_path()
    monkeypatch.setenv("DB_PATH", str(db_path))
    object.__setattr__(settings, "DB_PATH", str(db_path))
    _sync_config_singletons(manager)

    return {
        "project_root": str(project_root),
        "runtime_root": str(runtime_root),
        "db_path": str(db_path),
    }


@pytest.fixture
def relaxed_test_order_rules() -> None:
    original_rules = {
        "MIN_ORDER_NOTIONAL_KRW": float(settings.MIN_ORDER_NOTIONAL_KRW),
        "LIVE_MIN_ORDER_QTY": float(settings.LIVE_MIN_ORDER_QTY),
        "LIVE_ORDER_QTY_STEP": float(settings.LIVE_ORDER_QTY_STEP),
        "LIVE_ORDER_MAX_QTY_DECIMALS": int(settings.LIVE_ORDER_MAX_QTY_DECIMALS),
    }
    object.__setattr__(settings, "MIN_ORDER_NOTIONAL_KRW", 0.0)
    object.__setattr__(settings, "LIVE_MIN_ORDER_QTY", 0.0)
    object.__setattr__(settings, "LIVE_ORDER_QTY_STEP", 0.0)
    object.__setattr__(settings, "LIVE_ORDER_MAX_QTY_DECIMALS", 8)
    try:
        yield
    finally:
        for key, value in original_rules.items():
            object.__setattr__(settings, key, value)


@pytest.fixture(autouse=True)
def _restore_global_settings_state(tmp_path: Path):
    """Keep direct settings mutations from leaking across test modules."""
    from bithumb_bot.broker import order_rules as _order_rules
    from bithumb_bot.research import strategy_registry as _strategy_registry
    from bithumb_bot.research import validation_protocol as _validation_protocol

    keys = [
        "MODE",
        "DB_PATH",
        "STRATEGY_NAME",
        "SMA_SHORT",
        "SMA_LONG",
        "SMA_FILTER_GAP_MIN_RATIO",
        "SMA_FILTER_VOL_WINDOW",
        "SMA_FILTER_VOL_MIN_RANGE_RATIO",
        "SMA_FILTER_OVEREXT_LOOKBACK",
        "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO",
        "SMA_COST_EDGE_ENABLED",
        "SMA_COST_EDGE_MIN_RATIO",
        "START_CASH_KRW",
        "BUY_FRACTION",
        "MAX_ORDER_KRW",
        "MAX_DAILY_ORDER_COUNT",
        "MAX_OPEN_POSITIONS",
        "FEE_RATE",
        "PAPER_FEE_RATE",
        "LIVE_FEE_RATE_ESTIMATE",
        "SLIPPAGE_BPS",
        "STRATEGY_ENTRY_SLIPPAGE_BPS",
        "ENTRY_EDGE_BUFFER_RATIO",
        "STRATEGY_MIN_EXPECTED_EDGE_RATIO",
        "STRATEGY_EXIT_RULES",
        "STRATEGY_EXIT_STOP_LOSS_RATIO",
        "STRATEGY_EXIT_MAX_HOLDING_MIN",
        "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO",
        "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO",
        "MAX_ORDERBOOK_SPREAD_BPS",
        "PAPER_EXECUTION_MODEL",
        "PAPER_EXECUTION_STRESS_SEED",
        "PAPER_EXECUTION_LATENCY_MS",
        "PAPER_EXECUTION_PARTIAL_FILL_RATE",
        "PAPER_EXECUTION_PARTIAL_FILL_FRACTION",
        "PAPER_EXECUTION_ORDER_FAILURE_RATE",
        "MAX_MARKET_SLIPPAGE_BPS",
        "MIN_ORDER_NOTIONAL_KRW",
        "PRETRADE_BALANCE_BUFFER_BPS",
        "LIVE_DRY_RUN",
        "LIVE_REAL_ORDER_ARMED",
        "BITHUMB_API_KEY",
        "BITHUMB_API_SECRET",
        "MAX_DAILY_LOSS_KRW",
        "KILL_SWITCH",
        "MAX_OPEN_ORDER_AGE_SEC",
        "LIVE_MIN_ORDER_QTY",
        "LIVE_ORDER_QTY_STEP",
        "LIVE_ORDER_MAX_QTY_DECIMALS",
        "LIVE_PRICE_PROTECTION_MAX_SLIPPAGE_BPS",
        "LIVE_PRICE_REFERENCE_MAX_AGE_SEC",
        "EXECUTION_FILL_REFERENCE_POLICY",
        "EXECUTION_MISSING_QUOTE_POLICY",
        "EXECUTION_MIN_REALITY_LEVEL_FOR_PROMOTION",
        "EXECUTION_ALLOW_SAME_CANDLE_CLOSE_FILL",
        "EXECUTION_TOP_OF_BOOK_REQUIRED",
        "EXECUTION_DEPTH_REQUIRED",
        "EXECUTION_TRADE_TICK_REQUIRED",
        "EXECUTION_QUEUE_POSITION_REQUIRED",
        "EXECUTION_MARKET_IMPACT_REQUIRED",
        "EXECUTION_INTRA_CANDLE_PATH_AVAILABLE",
        "EXECUTION_LATENCY_MODEL_TYPE",
        "EXECUTION_LATENCY_MS",
        "EXECUTION_PARTIAL_FILL_MODEL_TYPE",
        "EXECUTION_PARTIAL_FILL_RATE",
        "EXECUTION_ORDER_FAILURE_MODEL_TYPE",
        "EXECUTION_ORDER_FAILURE_RATE",
        "EXECUTION_FEE_SOURCE",
        "EXECUTION_SLIPPAGE_SOURCE",
        "EXECUTION_CALIBRATION_REQUIRED",
        "EXECUTION_CALIBRATION_ARTIFACT_HASH",
        "SMA_MARKET_REGIME_ENABLED",
        "MIN_NET_EDGE_KRW",
        "MIN_MARGIN_AFTER_COST_RATIO",
        "PRE_TRADE_ECONOMICS_BLOCKING_ENABLED",
        "RESIDUAL_BUY_SIZING_MODE",
        "RESIDUAL_LIVE_SELL_MODE",
        "LIVE_FILL_FEE_STRICT_MODE",
        "LIVE_FILL_FEE_STRICT_MIN_NOTIONAL_KRW",
        "LIVE_FILL_FEE_ALERT_MIN_NOTIONAL_KRW",
        "LIVE_ALLOW_ORDER_RULE_FALLBACK",
        "BUY_PRICE_NONE_MARKET_TO_PRICE_ALIAS_ENABLED",
        "PAIR",
        "INTERVAL",
        "APPROVED_STRATEGY_PROFILE_PATH",
        "STRATEGY_APPROVED_PROFILE_PATH",
        "STRATEGY_CANDIDATE_PROFILE_PATH",
    ]
    test_path_manager = _path_manager_for_runtime_root((tmp_path / "runtime-default").resolve())
    _sync_config_singletons(test_path_manager)
    _strategy_registry.reload_research_strategy_plugins_for_tests()
    _validation_protocol._CANDIDATE_SCENARIO_WORKER_CONTEXT = None
    object.__setattr__(settings, "DB_PATH", str(test_path_manager.primary_db_path()))
    original = {key: getattr(settings, key) for key in keys if hasattr(settings, key)}
    _order_rules._cached_rules.clear()
    try:
        yield
    finally:
        _sync_config_singletons(test_path_manager)
        for key, value in original.items():
            object.__setattr__(settings, key, value)
        _sync_config_singletons(test_path_manager)
        _order_rules._cached_rules.clear()
        _strategy_registry.reload_research_strategy_plugins_for_tests()
        _validation_protocol._CANDIDATE_SCENARIO_WORKER_CONTEXT = None
