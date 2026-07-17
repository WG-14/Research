"""Compatibility lookup requiring an explicitly selected registry."""


def build_threshold_research_only_plugin(*, registry):
    return registry.resolve("threshold_research_only")


__all__ = ["build_threshold_research_only_plugin"]
