#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bithumb_research.config_spec import ENV_SPECS, SECRET_KEYS, SPEC_BY_NAME  # noqa: E402


ASSIGNMENT_RE = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")


def parse_env_example(text: str) -> dict[str, list[tuple[int, bool, str]]]:
    entries: dict[str, list[tuple[int, bool, str]]] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = ASSIGNMENT_RE.match(line)
        if not match:
            continue
        commented = bool(match.group(1))
        key = match.group(2)
        value = match.group(3).strip()
        entries.setdefault(key, []).append((line_no, commented, value))
    return entries


def _secret_value_is_safe(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    safe_fragments = ("your_", "xxx", "example", "hooks.slack.com/services/xxx")
    return any(fragment in lowered for fragment in safe_fragments)


def check_env_example_text(text: str) -> list[str]:
    failures: list[str] = []
    entries = parse_env_example(text)
    declared = set(SPEC_BY_NAME)

    undeclared = sorted(set(entries) - declared)
    if undeclared:
        failures.append(".env.example keys missing from ConfigSpec: " + ", ".join(undeclared))

    missing_operator_visible = sorted(
        spec.name
        for spec in ENV_SPECS
        if spec.operator_visible and not spec.deprecated and spec.name not in entries
    )
    if missing_operator_visible:
        failures.append("operator-visible ConfigSpec keys missing from .env.example: " + ", ".join(missing_operator_visible))

    for spec in ENV_SPECS:
        if spec.secret and spec.name in entries:
            for line_no, commented, value in entries[spec.name]:
                if not commented and not _secret_value_is_safe(value):
                    failures.append(f"secret key has unsafe .env.example value at line {line_no}: {spec.name}")
        if (spec.deprecated or spec.ignored) and spec.name in entries:
            for line_no, _, _ in entries[spec.name]:
                window = "\n".join(text.splitlines()[max(0, line_no - 4): min(len(text.splitlines()), line_no + 2)])
                if "deprecated" not in window.lower() and "ignored" not in window.lower():
                    failures.append(f"deprecated/ignored key lacks label near line {line_no}: {spec.name}")

    live_required = sorted(spec.name for spec in ENV_SPECS if spec.required_in_live)
    for key in live_required:
        if key not in entries:
            failures.append(f"live-required key missing from .env.example: {key}")
            continue
        if not any(not commented for _, commented, _ in entries[key]):
            failures.append(f"live-required key only appears in commented examples: {key}")

    for key in sorted(SECRET_KEYS):
        if key in entries:
            continue
        failures.append(f"secret key missing from .env.example: {key}")

    return failures


def check_env_example(path: Path = ENV_EXAMPLE) -> list[str]:
    if not path.exists():
        return [f"{path.relative_to(PROJECT_ROOT)} is missing"]
    return check_env_example_text(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify .env.example against ConfigSpec.")
    parser.add_argument("--check", action="store_true", help="Fail if .env.example violates the config contract.")
    args = parser.parse_args(argv)

    failures = check_env_example()
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print(".env.example contract check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
