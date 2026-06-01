#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "bithumb_bot"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
CONFIG_REFERENCE = PROJECT_ROOT / "docs" / "config-reference.md"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bithumb_bot.config_spec import ENV_SPECS, SPEC_BY_NAME  # noqa: E402


ENV_CALL_RE = re.compile(
    r"(?:os\.getenv|parse_bool_env|parse_bool_env_strict|parse_float_env|"
    r"parse_non_negative_float_env|parse_deprecated_ignored_bool_env)\(\s*[\"']([A-Z0-9_]+)[\"']"
)


def discover_env_reads() -> set[str]:
    keys: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        keys.update(ENV_CALL_RE.findall(text))
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    return keys


def env_example_keys() -> set[str]:
    keys: set[str] = set()
    assignment = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=")
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = assignment.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def _failures() -> list[str]:
    failures: list[str] = []
    declared = set(SPEC_BY_NAME)
    read_keys = discover_env_reads()
    example_keys = env_example_keys()

    missing_from_spec = sorted(read_keys - declared)
    if missing_from_spec:
        failures.append("env reads missing from config spec: " + ", ".join(missing_from_spec))

    undeclared_example = sorted(example_keys - declared)
    if undeclared_example:
        failures.append(".env.example keys missing from config spec: " + ", ".join(undeclared_example))

    docs_text = CONFIG_REFERENCE.read_text(encoding="utf-8") if CONFIG_REFERENCE.exists() else ""
    for spec in ENV_SPECS:
        if spec.operator_visible and spec.name not in docs_text:
            failures.append(f"operator-visible spec missing from config reference: {spec.name}")
        if spec.name in example_keys and (spec.deprecated or spec.ignored):
            pattern = re.compile(rf"{re.escape(spec.name)}=.*(?:deprecated|ignored)", re.IGNORECASE)
            comment_pattern = re.compile(rf"(?:deprecated|ignored).*{re.escape(spec.name)}", re.IGNORECASE)
            if not (pattern.search(ENV_EXAMPLE.read_text(encoding="utf-8")) or comment_pattern.search(ENV_EXAMPLE.read_text(encoding="utf-8"))):
                failures.append(f"deprecated/ignored key in .env.example is not labeled: {spec.name}")
        if spec.secret and spec.name in example_keys:
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{spec.name}="):
                    value = line.split("=", 1)[1].strip()
                    if value and "your_" not in value and "xxx" not in value and "hooks.slack.com" not in value:
                        failures.append(f"secret key has unsafe .env.example value: {spec.name}")

    required_live = sorted(spec.name for spec in ENV_SPECS if spec.required_in_live)
    live_preset_text = "\n".join(line for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() if "Live" in line or line.startswith("# ") or line.startswith("#"))
    for key in required_live:
        if key not in ENV_EXAMPLE.read_text(encoding="utf-8") and key not in live_preset_text:
            failures.append(f"live-required key missing from live examples/checklists: {key}")

    if "BUY_PRICE_NONE_MARKET_TO_PRICE_ALIAS_ENABLED" not in declared:
        failures.append("BUY_PRICE_NONE_MARKET_TO_PRICE_ALIAS_ENABLED not handled in spec")
    if "LIVE_ALLOW_ORDER_RULE_FALLBACK" not in declared:
        failures.append("LIVE_ALLOW_ORDER_RULE_FALLBACK not handled in spec")
    if "NOTIFIER_DEDUPE_WINDOW_SEC" not in declared:
        failures.append("NOTIFIER_DEDUPE_WINDOW_SEC not handled in spec")

    return failures


def main() -> int:
    failures = _failures()
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("env drift check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
