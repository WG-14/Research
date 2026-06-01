from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import fields

from bithumb_bot.config import Settings
from bithumb_bot.config_spec import SPEC_BY_NAME
from tools.check_env_drift import _failures
from tools.generate_config_docs import render_config_reference


def test_config_spec_covers_current_drift_candidates() -> None:
    assert SPEC_BY_NAME["BUY_PRICE_NONE_MARKET_TO_PRICE_ALIAS_ENABLED"].deprecated
    assert SPEC_BY_NAME["BUY_PRICE_NONE_MARKET_TO_PRICE_ALIAS_ENABLED"].ignored
    assert SPEC_BY_NAME["LIVE_ALLOW_ORDER_RULE_FALLBACK"].deprecated
    assert SPEC_BY_NAME["LIVE_ALLOW_ORDER_RULE_FALLBACK"].ignored
    assert SPEC_BY_NAME["NOTIFIER_DEDUPE_WINDOW_SEC"].operator_visible


def test_env_drift_checker_passes() -> None:
    assert _failures() == []


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
