"""Retired compatibility placeholder for private helper imports."""

from __future__ import annotations


def legacy_specs(*_args, **_kwargs):
    raise RuntimeError("legacy CLI specs are retired; define CommandSpec in the command module")
