from __future__ import annotations

from types import SimpleNamespace

import pytest

from bithumb_bot.live_pipeline_smoke_preflight import (
    LivePipelineSmokePreflightError,
    LivePipelineSmokeReadiness,
    validate_live_pipeline_smoke_start_preflight,
)


class _Broker:
    def __init__(self, open_order=False):
        self.open_order = open_order

    def get_open_orders(self):
        if not self.open_order:
            return []
        return [SimpleNamespace(status="NEW")]


def _cfg():
    return SimpleNamespace(
        MODE="live",
        LIVE_DRY_RUN=False,
        LIVE_REAL_ORDER_ARMED=True,
        KILL_SWITCH=False,
        EXECUTION_ENGINE="target_delta",
        PAIR="KRW-BTC",
        BITHUMB_API_KEY="key",
        BITHUMB_API_SECRET="secret",
        DB_PATH="/tmp/live.sqlite",
    )


def _conn(open_orders=0):
    class Conn:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(fetchone=lambda: (open_orders,))

    return Conn()


def _snapshot(**overrides):
    values = {
        "broker_position_evidence": {
            "broker_qty_known": True,
            "balance_source_stale": False,
            "broker_qty": 0.0,
        },
        "projection_convergence": {
            "converged": True,
            "portfolio_qty": 0.0,
            "projected_total_qty": 0.0,
        },
        "open_order_count": 0,
        "submit_unknown_count": 0,
        "recovery_required_count": 0,
        "fee_pending_count": 0,
        "active_fee_accounting_blocker": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("snapshot_overrides", "match"),
    [
        ({"submit_unknown_count": 1}, "submit_unknown"),
        ({"recovery_required_count": 1}, "recovery_required"),
        ({"fee_pending_count": 1}, "fee_pending"),
        ({"active_fee_accounting_blocker": True}, "active_fee_accounting_blocker"),
        ({"projection_convergence": {"converged": False}}, "projection_non_converged"),
        ({"broker_position_evidence": {"broker_qty_known": False}}, "broker_qty_evidence"),
        ({"broker_position_evidence": {"broker_qty_known": True, "balance_source_stale": True}}, "broker_qty_evidence"),
    ],
)
def test_preflight_blocks_runtime_readiness_issues(snapshot_overrides, match) -> None:
    with pytest.raises(LivePipelineSmokePreflightError, match=match):
        validate_live_pipeline_smoke_start_preflight(
            cfg=_cfg(),
            conn=_conn(),
            broker=_Broker(),
            market="KRW-BTC",
            readiness_builder=lambda _conn: _snapshot(**snapshot_overrides),
            market_preflight=lambda _cfg: None,
            cli_guard=lambda _cfg: None,
            schema_validator=lambda _conn: None,
        )


def test_preflight_blocks_open_local_and_broker_orders() -> None:
    with pytest.raises(LivePipelineSmokePreflightError, match="open_local_order"):
        validate_live_pipeline_smoke_start_preflight(
            cfg=_cfg(),
            conn=_conn(open_orders=1),
            broker=_Broker(),
            market="KRW-BTC",
            readiness_builder=lambda _conn: _snapshot(),
            market_preflight=lambda _cfg: None,
            cli_guard=lambda _cfg: None,
            schema_validator=lambda _conn: None,
        )

    with pytest.raises(LivePipelineSmokePreflightError, match="open_broker_order"):
        validate_live_pipeline_smoke_start_preflight(
            cfg=_cfg(),
            conn=_conn(),
            broker=_Broker(open_order=True),
            market="KRW-BTC",
            readiness_builder=lambda _conn: _snapshot(),
            market_preflight=lambda _cfg: None,
            cli_guard=lambda _cfg: None,
            schema_validator=lambda _conn: None,
        )
