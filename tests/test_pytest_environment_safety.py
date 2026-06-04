from __future__ import annotations

import os
import subprocess
import sys

import pytest

from bithumb_bot import notifier
from bithumb_bot.config import settings
from bithumb_bot.config_spec import PYTEST_INHERITANCE_UNSAFE_ENV_KEYS


def test_pytest_policy_clears_side_effect_env_at_test_start() -> None:
    for key in PYTEST_INHERITANCE_UNSAFE_ENV_KEYS:
        assert os.getenv(key) is None
    assert os.getenv("NOTIFIER_ENABLED") == "false"
    assert settings.BITHUMB_API_KEY == ""
    assert settings.BITHUMB_API_SECRET == ""


def test_conftest_prevents_import_time_settings_secret_snapshot() -> None:
    code = """
import os
import tests.conftest
from bithumb_bot.config import settings
assert os.getenv("BITHUMB_API_KEY") is None
assert os.getenv("BITHUMB_API_SECRET") is None
assert settings.BITHUMB_API_KEY == ""
assert settings.BITHUMB_API_SECRET == ""
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": "src:.",
            "BITHUMB_API_KEY": "parent-real-key",
            "BITHUMB_API_SECRET": "parent-real-secret",
            "NTFY_TOPIC": "parent-topic",
        },
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_pytest_policy_blocks_notification_transports_even_if_env_reappears(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOTIFIER_ENABLED", "true")
    monkeypatch.setenv("NTFY_TOPIC", "parent-topic")
    monkeypatch.setenv("NOTIFIER_WEBHOOK_URL", "https://example.invalid/generic")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.invalid/slack")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "parent-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "parent-chat")

    with pytest.raises(
        notifier.PytestNotificationSafetyViolation,
        match="external ntfy transport is disabled in pytest",
    ):
        notifier.notify("policy check")

    assert capsys.readouterr().out == ""


def test_pytest_policy_blocks_direct_notifier_transport_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(notifier.PytestNotificationSafetyViolation, match="external notification transport is disabled in pytest"):
        notifier._post_json("https://example.invalid/generic", {"text": "blocked"})
    monkeypatch.setenv("NTFY_TOPIC", "parent-topic")
    with pytest.raises(notifier.PytestNotificationSafetyViolation, match="external ntfy transport is disabled in pytest"):
        notifier._post_ntfy("blocked", severity=notifier.AlertSeverity.WARN)
