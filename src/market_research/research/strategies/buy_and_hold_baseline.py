"""Compatibility lookup requiring an explicitly selected registry."""
def build_buy_and_hold_baseline_plugin(*, registry):
    return registry.resolve("buy_and_hold_baseline")

__all__ = ["build_buy_and_hold_baseline_plugin"]
