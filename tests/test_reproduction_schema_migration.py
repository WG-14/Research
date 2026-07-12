from __future__ import annotations
import json
import pytest
from market_research.research.reproduction import ReproductionContractError, load_reproduction_receipt


def test_unknown_receipt_schema_version_is_rejected(tmp_path) -> None:
    path = tmp_path / "legacy.json"; path.write_text(json.dumps({"schema_version": 999}))
    with pytest.raises(ReproductionContractError, match="unsupported"):
        load_reproduction_receipt(path)
