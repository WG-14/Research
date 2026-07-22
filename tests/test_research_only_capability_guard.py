from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = {
    "core": ROOT / "src" / "market_research",
    "web": ROOT / "apps" / "internal_web" / "src",
    "operations": ROOT / "services" / "research_operations" / "src",
}
ORDERBOOK_INPUT_MODULES = (
    ROOT / "src" / "market_research" / "orderbook_top_store.py",
    ROOT / "src" / "market_research" / "orderbook_depth_store.py",
)

_KNOWN_TRADING_CLIENT_MODULES = frozenset(
    {
        "alpaca",
        "binance",
        "bithumb",
        "bitmex",
        "bybit",
        "ccxt",
        "coinbase",
        "ib_insync",
        "ibapi",
        "krakenex",
        "kucoin",
        "oandapyv20",
        "okx",
        "pyupbit",
        "robin_stocks",
        "schwab",
        "tastytrade",
        "webull",
    }
)
_KNOWN_TRADING_CLIENT_DEPENDENCIES = frozenset(
    {
        "alpaca-py",
        "ccxt",
        "coinbase-advanced-py",
        "ib-insync",
        "ibapi",
        "krakenex",
        "oandapyv20",
        "pyupbit",
        "python-binance",
        "robin-stocks",
        "schwab-py",
        "tastytrade",
    }
)
_OUTBOUND_NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "httpx",
        "requests",
        "socket",
        "urllib.request",
        "websocket",
        "websockets",
    }
)
_OPERATIONS_NETWORK_ALLOWLIST = frozenset(
    {
        ROOT
        / "services"
        / "research_operations"
        / "src"
        / "research_operations"
        / "alerting.py"
    }
)
_RUNTIME_SEQUENCE_ALLOWLIST = {
    ROOT / "scripts" / "make_failure_packet.sh": frozenset(
        {
            ("broker",),
            ("order", "submission"),
            ("order", "management"),
            ("real", "account"),
        }
    )
}
_FORBIDDEN_CAPABILITY_SEQUENCES = {
    "broker_access": (("broker",), ("brokerage",)),
    "live_account": (
        ("account", "access"),
        ("account", "client"),
        ("account", "connected"),
        ("live", "account"),
        ("real", "account"),
        ("trading", "account"),
    ),
    "order_routing": (
        ("cancel", "order"),
        ("live", "order"),
        ("order", "router"),
        ("order", "submission"),
        ("order", "management"),
        ("replace", "order"),
        ("submit", "order"),
    ),
    "private_exchange": (
        ("exchange", "private"),
        ("private", "exchange"),
    ),
    "network_market_data": (
        ("download", "market", "data"),
        ("fetch", "market", "data"),
        ("market", "data", "collection"),
        ("market", "data", "collector"),
        ("market", "data", "fetcher"),
        ("network", "market", "data"),
        ("retry", "backfill"),
        ("source", "probe"),
    ),
    "operational_order_fill_ingestion": (
        ("operational", "order", "fill"),
        ("order", "fill", "database", "ingestion"),
    ),
    "exchange_order_semantics": (("exchange", "raw", "order", "semantics"),),
    "reviewed_account_profile": (("reviewed", "account", "profile"),),
    "runtime_trading": (
        ("live", "trading"),
        ("runtime", "trading", "strategy"),
    ),
    "emergency_account_control": (("emergency", "account", "control"),),
}
_FORBIDDEN_NETWORK_CALLS = frozenset(
    {
        "cancel_order",
        "create_order",
        "fetch_balance",
        "fetch_ohlcv",
        "fetch_open_orders",
        "fetch_order_book",
        "fetch_positions",
        "get_account",
        "list_accounts",
        "replace_order",
        "route_order",
        "submit_order",
    }
)
_CONFIG_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}")
_SQL_MUTATION = re.compile(
    r"^\s*(?:ALTER|CREATE|DELETE|DROP|INSERT|REPLACE|UPDATE)\b",
    re.IGNORECASE,
)
_RUNTIME_CONFIGURATION_SUFFIXES = frozenset(
    {
        ".conf",
        ".env",
        ".ini",
        ".json",
        ".service",
        ".socket",
        ".timer",
        ".toml",
        ".yaml",
        ".yml",
    }
)


def _normalized_words(value: object) -> tuple[str, ...]:
    raw = str(value).strip()
    acronym_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", acronym_split)
    return tuple(
        part.lower() for part in re.split(r"[^A-Za-z0-9]+", camel_split) if part
    )


def _contains_sequence(words: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    size = len(sequence)
    return any(words[index : index + size] == sequence for index in range(len(words)))


def _forbidden_capability(value: object) -> str | None:
    words = _normalized_words(value)
    for capability, sequences in _FORBIDDEN_CAPABILITY_SEQUENCES.items():
        if any(_contains_sequence(words, sequence) for sequence in sequences):
            return capability
    return None


def _module_matches(module: str, candidates: Iterable[str]) -> bool:
    return any(module == item or module.startswith(item + ".") for item in candidates)


def _imports(tree: ast.AST) -> tuple[str, ...]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return tuple(sorted(names))


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _constant_first_argument(node: ast.Call) -> str | None:
    if not node.args:
        return None
    value = node.args[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _environment_key(node: ast.Call) -> str | None:
    function = node.func
    name = _call_name(node)
    if name is None:
        return None
    direct_reader = (
        isinstance(function, ast.Attribute)
        and function.attr == "getenv"
        and isinstance(function.value, ast.Name)
        and function.value.id == "os"
    )
    environ_get = (
        isinstance(function, ast.Attribute)
        and function.attr == "get"
        and (
            (
                isinstance(function.value, ast.Attribute)
                and function.value.attr == "environ"
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id == "os"
            )
            or (
                isinstance(function.value, ast.Name)
                and function.value.id in {"env", "environ"}
            )
        )
    )
    local_reader = name.startswith("_env_") or name.endswith("_env")
    if direct_reader or environ_get or local_reader:
        return _constant_first_argument(node)
    return None


def _python_source_violations(*, distribution: str, path: Path) -> list[str]:
    violations: list[str] = []
    relative = path.relative_to(ROOT).as_posix()
    path_capability = _forbidden_capability(relative)
    if path_capability is not None:
        violations.append(f"{relative}:path:{path_capability}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for imported in _imports(tree):
        if _module_matches(imported, _KNOWN_TRADING_CLIENT_MODULES):
            violations.append(f"{relative}:import:{imported}")
        if _module_matches(imported, _OUTBOUND_NETWORK_MODULES) and not (
            distribution == "operations" and path in _OPERATIONS_NETWORK_ALLOWLIST
        ):
            violations.append(f"{relative}:outbound-network:{imported}")
        capability = _forbidden_capability(imported)
        if capability is not None:
            violations.append(f"{relative}:import:{capability}:{imported}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            capability = _forbidden_capability(node.name)
            if capability is not None:
                violations.append(
                    f"{relative}:{node.lineno}:symbol:{capability}:{node.name}"
                )
        elif isinstance(node, ast.Call):
            call_name = _call_name(node)
            normalized_call = "_".join(_normalized_words(call_name or ""))
            if normalized_call in _FORBIDDEN_NETWORK_CALLS:
                violations.append(f"{relative}:{node.lineno}:call:{normalized_call}")
            environment_key = _environment_key(node)
            capability = _forbidden_capability(environment_key or "")
            if capability is not None:
                violations.append(
                    f"{relative}:{node.lineno}:environment:{capability}:"
                    f"{environment_key}"
                )
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            capability = _forbidden_capability(node.slice.value)
            if capability is not None:
                violations.append(
                    f"{relative}:{node.lineno}:environment:{capability}:"
                    f"{node.slice.value}"
                )
    return violations


def _repository_candidate_paths() -> tuple[Path, ...]:
    raw_paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=ROOT,
    ).split(b"\0")
    paths = {
        ROOT / Path(raw_path.decode("utf-8")) for raw_path in raw_paths if raw_path
    }
    return tuple(sorted(path for path in paths if path.is_file()))


def _is_runtime_surface(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    parts = relative.parts
    if not parts or parts[0] in {"docs", "tests"}:
        return False
    if relative == Path(".env.example"):
        return True
    if parts[:2] == (".github", "workflows"):
        return True
    if parts[0] == "scripts":
        return path.suffix.lower() not in {".md", ".txt"}
    if len(parts) == 1 and path.suffix.lower() in {".py", ".sh"}:
        return True
    if parts[:2] == ("services", "research_operations"):
        if relative.name in {"Dockerfile", "Containerfile"}:
            return True
        if len(parts) >= 3 and parts[2] in {"config", "deploy", "scripts"}:
            return path.suffix.lower() not in {".md", ".txt"}
    if relative.name in {"Dockerfile", "Containerfile", "pyproject.toml"}:
        return True
    if relative.name.startswith("requirements") and path.suffix == ".txt":
        return True
    return path.suffix.lower() in _RUNTIME_CONFIGURATION_SUFFIXES


def _runtime_surface_paths() -> tuple[Path, ...]:
    return tuple(
        path for path in _repository_candidate_paths() if _is_runtime_surface(path)
    )


def _source_violations() -> list[str]:
    sources: dict[Path, str] = {}
    for distribution, source_root in SOURCE_ROOTS.items():
        sources.update((path, distribution) for path in source_root.rglob("*.py"))
    for path in _repository_candidate_paths():
        relative = path.relative_to(ROOT)
        if (
            path.suffix != ".py"
            or relative.parts[0] in {"docs", "tests"}
            or path in sources
        ):
            continue
        if path.is_relative_to(ROOT / "services" / "research_operations"):
            distribution = "operations"
        elif path.is_relative_to(ROOT / "apps" / "internal_web"):
            distribution = "web"
        else:
            distribution = "support"
        sources[path] = distribution
    violations: list[str] = []
    for path, distribution in sorted(sources.items()):
        violations.extend(
            _python_source_violations(distribution=distribution, path=path)
        )
    return violations


def _runtime_surface_violations() -> list[str]:
    violations: list[str] = []
    for path in _runtime_surface_paths():
        if path.suffix == ".py":
            continue
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        words = _normalized_words(text)
        allowed_sequences = _RUNTIME_SEQUENCE_ALLOWLIST.get(path, frozenset())
        for capability, sequences in _FORBIDDEN_CAPABILITY_SEQUENCES.items():
            for sequence in sequences:
                if sequence not in allowed_sequences and _contains_sequence(
                    words, sequence
                ):
                    violations.append(
                        f"{relative}:runtime:{capability}:{'_'.join(sequence)}"
                    )
        for call_name in _FORBIDDEN_NETWORK_CALLS:
            if _contains_sequence(words, tuple(call_name.split("_"))):
                violations.append(f"{relative}:runtime-call:{call_name}")
        for token in _CONFIG_TOKEN.findall(text):
            normalized = token.lower().replace("_", "-")
            if normalized in _KNOWN_TRADING_CLIENT_DEPENDENCIES:
                violations.append(f"{relative}:trading-client:{normalized}")
    return violations


def _tracked_configuration_paths() -> tuple[Path, ...]:
    result: list[Path] = []
    for path in _repository_candidate_paths():
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"docs", "tests"}:
            continue
        if (
            relative.name == ".env.example"
            or relative.suffix.lower() in {".json", ".toml", ".yaml", ".yml"}
            or (relative.name.startswith("requirements") and relative.suffix == ".txt")
        ):
            result.append(path)
    return tuple(sorted(result))


def _dependency_specs(payload: dict[str, object]) -> Iterable[str]:
    project = payload.get("project")
    if isinstance(project, dict):
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            yield from (str(item) for item in dependencies)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for values in optional.values():
                if isinstance(values, list):
                    yield from (str(item) for item in values)
    groups = payload.get("dependency-groups")
    if isinstance(groups, dict):
        for values in groups.values():
            if isinstance(values, list):
                yield from (str(item) for item in values)


def _dependency_name(specification: str) -> str:
    return (
        re.split(r"[\s\[<>=!~;@]", specification, maxsplit=1)[0]
        .lower()
        .replace("_", "-")
    )


def _configuration_violations() -> list[str]:
    violations: list[str] = []
    for path in _tracked_configuration_paths():
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for token in _CONFIG_TOKEN.findall(text):
            capability = _forbidden_capability(token)
            if capability is not None:
                violations.append(f"{relative}:config:{capability}:{token}")
            normalized = token.lower().replace("_", "-")
            if normalized in _KNOWN_TRADING_CLIENT_DEPENDENCIES:
                violations.append(f"{relative}:trading-client:{normalized}")
        if path.name == "pyproject.toml":
            payload = tomllib.loads(text)
            for specification in _dependency_specs(payload):
                dependency = _dependency_name(specification)
                if dependency in _KNOWN_TRADING_CLIENT_DEPENDENCIES:
                    violations.append(f"{relative}:dependency:{dependency}")
    return violations


def test_forbidden_capability_guard_normalizes_common_alias_forms() -> None:
    aliases = {
        "brokerAPIKey": "broker_access",
        "live-account": "live_account",
        "orderRouter": "order_routing",
        "private_exchange": "private_exchange",
        "networkMarketDataCollection": "network_market_data",
        "retry-backfill-source-probe": "network_market_data",
    }
    assert {value: _forbidden_capability(value) for value in aliases} == aliases
    assert all(
        _forbidden_capability(value) is None
        for value in (
            "accountingLedger",
            "marketDataKnowledgeTime",
            "orderbookDepth",
            "OrderIntent",
            "researchJobWorker",
        )
    )


def test_all_distributions_and_runtime_configs_exclude_trading_capabilities() -> None:
    assert _source_violations() == []
    assert _runtime_surface_violations() == []
    assert _configuration_violations() == []


def test_guard_covers_non_python_operational_surfaces_without_tests_or_docs() -> None:
    relative_paths = {
        path.relative_to(ROOT).as_posix() for path in _runtime_surface_paths()
    }

    assert {
        ".github/workflows/research-ci.yml",
        "scripts/platform",
        "services/research_operations/Dockerfile",
        "services/research_operations/config/monitoring-policy.json",
        "services/research_operations/deploy/compose.yaml",
        (
            "services/research_operations/deploy/native/systemd/"
            "research-operations-job-worker.service"
        ),
        "services/research_operations/scripts/runtime-entrypoint.py",
        "src/market_research/builtin_strategies/sma_with_filter.strategy.json",
    } <= relative_paths
    assert not any(
        relative.startswith(("docs/", "tests/")) for relative in relative_paths
    )


def test_external_orderbook_input_modules_have_no_mutation_path() -> None:
    violations: list[str] = []
    for path in ORDERBOOK_INPUT_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                words = set(_normalized_words(node.name))
                if words & {"delete", "insert", "mutate", "replace", "upsert", "write"}:
                    violations.append(f"{path.name}:{node.lineno}:symbol:{node.name}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _SQL_MUTATION.search(node.value)
            ):
                violations.append(f"{path.name}:{node.lineno}:sql")

    assert violations == []
