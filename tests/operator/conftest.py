from __future__ import annotations

from dataclasses import fields

import pytest

from bithumb_research.config import settings


@pytest.fixture(autouse=True)
def _restore_operator_settings_state(monkeypatch: pytest.MonkeyPatch):
    """Keep operator command tests from leaking settings mutations."""
    original_values = {field.name: getattr(settings, field.name) for field in fields(type(settings))}

    monkeypatch.setenv("MODE", "paper")
    object.__setattr__(settings, "MODE", "paper")
    monkeypatch.setattr("bithumb_research.operator_commands.write_json_atomic", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("bithumb_research.reporting.write_json_atomic", lambda *_args, **_kwargs: None)

    try:
        yield
    finally:
        for key, value in original_values.items():
            object.__setattr__(settings, key, value)
