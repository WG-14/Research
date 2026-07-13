"""Compatibility lookup requiring an explicitly selected registry."""
def build_sma_with_filter_plugin(*, registry):
    return registry.resolve("sma_with_filter")

__all__ = ["build_sma_with_filter_plugin"]
