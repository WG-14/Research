"""Compatibility lookup requiring an explicitly selected registry."""


def build_noop_baseline_plugin(*, registry):
    return registry.resolve("noop_baseline")


__all__ = ["build_noop_baseline_plugin"]
