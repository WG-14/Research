from __future__ import annotations
import pytest
from market_research.research.datasets.locators import LocatorValidationError, parse_immutable_locator


def _locator(path: str = "/tmp/content/candles.sqlite") -> dict[str, str]:
    return {"type":"content_addressed_local", "path":path, "artifact_manifest_hash":"sha256:" + "a" * 64, "artifact_content_hash":"sha256:" + "b" * 64}


@pytest.mark.parametrize("path", ["datasets/latest.sqlite", "/datasets/current/c.sqlite", "relative.sqlite"])
def test_mutable_or_relative_locator_is_rejected(path: str) -> None:
    with pytest.raises(LocatorValidationError): parse_immutable_locator(_locator(path))


def test_unknown_locator_and_identity_less_locator_fail_closed() -> None:
    with pytest.raises(LocatorValidationError): parse_immutable_locator({"type":"unknown"})
    bad = _locator(); del bad["artifact_content_hash"]
    with pytest.raises(LocatorValidationError): parse_immutable_locator(bad)


def test_content_addressed_local_locator_round_trips() -> None:
    assert parse_immutable_locator(_locator()).as_dict() == _locator()
