from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import fields

from bithumb_bot.config import Settings
from bithumb_bot.config_spec import PYTEST_INHERITANCE_UNSAFE_ENV_KEYS, SPEC_BY_NAME
from tools.check_env_drift import _failures
from tools.generate_config_docs import render_config_reference
from tools.generate_env_example import check_env_example, check_env_example_text


def test_config_spec_covers_current_drift_candidates() -> None:
    assert SPEC_BY_NAME["BUY_PRICE_NONE_MARKET_TO_PRICE_ALIAS_ENABLED"].deprecated
    assert SPEC_BY_NAME["BUY_PRICE_NONE_MARKET_TO_PRICE_ALIAS_ENABLED"].ignored
    assert SPEC_BY_NAME["LIVE_ALLOW_ORDER_RULE_FALLBACK"].deprecated
    assert SPEC_BY_NAME["LIVE_ALLOW_ORDER_RULE_FALLBACK"].ignored
    assert SPEC_BY_NAME["NOTIFIER_DEDUPE_WINDOW_SEC"].operator_visible


def test_config_spec_pins_bithumb_api_secret_policy() -> None:
    spec = SPEC_BY_NAME["BITHUMB_API_SECRET"]
    assert spec.secret is True
    assert spec.required_in_live is True
    assert spec.validation_kind == "jwt_hs256_secret"
    assert spec.min_live_bytes == 32


def test_config_spec_classifies_pytest_unsafe_side_effect_env() -> None:
    expected_notification_keys = {
        "NTFY_TOPIC",
        "NOTIFIER_WEBHOOK_URL",
        "SLACK_WEBHOOK_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    }
    assert expected_notification_keys <= PYTEST_INHERITANCE_UNSAFE_ENV_KEYS
    for key in expected_notification_keys:
        assert SPEC_BY_NAME[key].side_effect_class == "external_notification"

    assert SPEC_BY_NAME["BITHUMB_API_KEY"].side_effect_class == "broker_private"
    assert SPEC_BY_NAME["BITHUMB_API_SECRET"].side_effect_class == "broker_private"
    assert {"BITHUMB_API_KEY", "BITHUMB_API_SECRET"} <= PYTEST_INHERITANCE_UNSAFE_ENV_KEYS


def test_env_drift_checker_passes() -> None:
    assert _failures() == []


def test_env_example_contract_is_in_sync() -> None:
    assert check_env_example() == []


def test_env_example_contract_rejects_undeclared_key() -> None:
    text = "MODE=paper\nUNKNOWN_BOT_SETTING=true\n"
    failures = check_env_example_text(text)
    assert any(".env.example keys missing from ConfigSpec: UNKNOWN_BOT_SETTING" in item for item in failures)


def test_env_example_contract_rejects_unsafe_secret_default() -> None:
    text = "MODE=paper\nBITHUMB_API_KEY=real-looking-token\n"
    failures = check_env_example_text(text)
    assert any("secret key has unsafe .env.example value" in item for item in failures)


def test_env_example_contract_rejects_unlabeled_deprecated_key() -> None:
    text = "MODE=paper\nLIVE_ALLOW_ORDER_RULE_FALLBACK=true\n"
    failures = check_env_example_text(text)
    assert any("deprecated/ignored key lacks label" in item for item in failures)


def test_config_reference_is_in_sync() -> None:
    with open("docs/config-reference.md", encoding="utf-8") as handle:
        assert handle.read() == render_config_reference()


def test_settings_restore_fixture_tracks_all_settings_fields() -> None:
    expected = {field.name for field in fields(Settings)}
    assert expected
    # The autouse fixture restores every dataclass field dynamically; this guard
    # makes future manual restore-list regressions visible.
    assert "LIVE_SUBMIT_CONTRACT_PROFILE" in expected


def test_env_drift_tool_cli_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "tools/check_env_drift.py"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_pytest_env_safety_tool_cli_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "tools/check_pytest_env_safety.py"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_env_example_tool_cli_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "tools/generate_env_example.py", "--check"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
