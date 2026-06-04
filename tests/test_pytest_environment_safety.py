from __future__ import annotations

import os

import pytest

from bithumb_bot import notifier
from bithumb_bot.config_spec import PYTEST_INHERITANCE_UNSAFE_ENV_KEYS


def test_pytest_policy_clears_side_effect_env_at_test_start() -> None:
    for key in PYTEST_INHERITANCE_UNSAFE_ENV_KEYS:
        assert os.getenv(key) is None
    assert os.getenv("NOTIFIER_ENABLED") == "false"


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

    notifier.notify("policy check")

    out = capsys.readouterr().out
    assert "[NOTIFY] ntfy delivery failed: RuntimeError" in out
    assert "[NOTIFY] generic webhook delivery failed: RuntimeError" in out
    assert "[NOTIFY] slack delivery failed: RuntimeError" in out
    assert "[NOTIFY] telegram delivery failed: RuntimeError" in out
    assert "[NOTIFY] policy check" in out


def test_pytest_policy_blocks_direct_notifier_transport_calls() -> None:
    with pytest.raises(RuntimeError, match="external notification transport is disabled in pytest"):
        notifier._post_json("https://example.invalid/generic", {"text": "blocked"})
    with pytest.raises(RuntimeError, match="external ntfy transport is disabled in pytest"):
        notifier._post_ntfy("blocked", severity=notifier.AlertSeverity.WARN)
