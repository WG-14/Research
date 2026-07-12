from __future__ import annotations
import pytest
from market_research.research.experiment_manifest import parse_manifest


def test_legacy_frozen_manifest_is_rejected_by_normal_loader_shape() -> None:
    payload = {"experiment_id":"x","hypothesis":"x","strategy_name":"noop_baseline","research_classification":"research_only","market":"KRW-BTC","interval":"1m","dataset":{"source":"frozen_sqlite_candles","snapshot_id":"x","train":{"start":"2026-01-01","end":"2026-01-01"},"validation":{"start":"2026-01-02","end":"2026-01-02"}},"parameter_space":{"NOOP_DECISION_START_INDEX":[0]},"cost_model":{"fee_rate":0,"slippage_bps":[0]},"acceptance_gate":{"min_trade_count":0,"max_mdd_pct":100,"min_profit_factor":0,"oos_return_must_be_positive":False,"parameter_stability_required":False,"final_holdout_required_for_validation":False}}
    with pytest.raises(ValueError, match="artifact_manifest_reference"):
        parse_manifest(payload)
