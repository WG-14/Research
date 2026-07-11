from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _deny(*args: object, **kwargs: object) -> None:
        raise RuntimeError("external network is disabled in tests")

    monkeypatch.setattr(socket, "create_connection", _deny)
