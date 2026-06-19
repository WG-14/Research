from __future__ import annotations

from bithumb_bot.cli.commands import live_ops


def test_smoke_buy_command_registered_in_live_ops() -> None:
    names = {spec.name for spec in live_ops.command_specs()}

    assert "smoke-buy" in names
    assert "flatten-position" in names


def test_smoke_buy_uses_operator_execution_smoke_guard() -> None:
    specs = {spec.name: spec for spec in live_ops.command_specs()}

    assert specs["smoke-buy"].guard_policy == "operator_execution_smoke"
