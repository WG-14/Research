from __future__ import annotations
import json
import pytest
from market_research.research.reproduction import (
    ReproductionContractError,
    load_reproduction_receipt,
)


def test_unknown_receipt_schema_version_is_rejected(tmp_path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"schema_version": 999}))
    with pytest.raises(ReproductionContractError, match="unsupported"):
        load_reproduction_receipt(path)


@pytest.mark.parametrize("schema", (1, 2, 7))
def test_legacy_receipt_schemas_are_rejected(tmp_path, schema: int) -> None:
    path = tmp_path / f"legacy-{schema}.json"
    path.write_text(json.dumps({"schema_version": schema}))
    with pytest.raises(ReproductionContractError, match="unsupported"):
        load_reproduction_receipt(path)
