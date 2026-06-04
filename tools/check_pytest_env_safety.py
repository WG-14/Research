#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bithumb_bot.config_spec import EXTERNAL_NOTIFICATION_ENV_KEYS, PYTEST_INHERITANCE_UNSAFE_ENV_KEYS  # noqa: E402


RUNNER = PROJECT_ROOT / "scripts" / "run_full_pytest_tests.sh"
CONFTEST = PROJECT_ROOT / "tests" / "conftest.py"


def _runner_unset_keys(text: str) -> set[str]:
    return set(re.findall(r"^\s*unset\s+([A-Z0-9_]+)\s*$", text, flags=re.MULTILINE))


def _failures() -> list[str]:
    failures: list[str] = []
    runner_text = RUNNER.read_text(encoding="utf-8")
    conftest_text = CONFTEST.read_text(encoding="utf-8")

    if "BITHUMB_PYTEST_ALLOW_EXTERNAL_NOTIFICATIONS" not in runner_text:
        failures.append("full pytest runner lacks explicit external-notification opt-in guard")
    if "export NOTIFIER_ENABLED=false" not in runner_text:
        failures.append("full pytest runner does not disable notifier delivery by default")

    missing_runner_unsets = sorted(EXTERNAL_NOTIFICATION_ENV_KEYS - _runner_unset_keys(runner_text))
    if missing_runner_unsets:
        failures.append("full pytest runner does not unset external notification env: " + ", ".join(missing_runner_unsets))

    try:
        pythonpath_index = runner_text.index('export PYTHONPATH="${PWD}${PYTHONPATH:+:${PYTHONPATH}}"')
        safety_index = runner_text.index("BITHUMB_PYTEST_ALLOW_EXTERNAL_NOTIFICATIONS")
        preflight_index = runner_text.index("bithumb_pytest_run_preflight")
    except ValueError as exc:
        failures.append(f"full pytest runner missing expected ordering marker: {exc}")
    else:
        if not (pythonpath_index < safety_index < preflight_index):
            failures.append("full pytest runner must sanitize notification env after PYTHONPATH and before preflight")

    if "PYTEST_INHERITANCE_UNSAFE_ENV_KEYS" not in conftest_text:
        failures.append("pytest conftest does not use the config-spec unsafe inheritance key set")
    if "monkeypatch.delenv(key" not in conftest_text:
        failures.append("pytest conftest does not clear unsafe inherited env")
    if "monkeypatch.setenv(\"NOTIFIER_ENABLED\", \"false\")" not in conftest_text:
        failures.append("pytest conftest does not disable notifier delivery by default")
    if "_post_json" not in conftest_text or "_post_ntfy" not in conftest_text:
        failures.append("pytest conftest does not guard notifier transport functions")

    missing_specs = sorted(
        key
        for key in {
            "NTFY_TOPIC",
            "NOTIFIER_WEBHOOK_URL",
            "SLACK_WEBHOOK_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        }
        if key not in PYTEST_INHERITANCE_UNSAFE_ENV_KEYS
    )
    if missing_specs:
        failures.append("config spec does not classify required pytest-unsafe env: " + ", ".join(missing_specs))

    return failures


def main() -> int:
    failures = _failures()
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("pytest env safety check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
