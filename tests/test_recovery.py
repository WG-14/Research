from __future__ import annotations

import json

import pytest

from bithumb_bot import runtime_state
from bithumb_bot.broker.accounts_v1 import (
    AccountsRequiredCurrencyMissingError,
    parse_accounts_response,
    select_pair_balances,
    to_broker_balance,
)
from bithumb_bot.broker.balance_source import AccountsV1BalanceSource, BalanceSnapshot
from bithumb_bot.broker.base import BrokerFill, BrokerOrder
from bithumb_bot.config import settings
from bithumb_bot.db_core import ensure_db, get_portfolio_breakdown, set_portfolio_breakdown
from bithumb_bot.execution import record_order_if_missing
from bithumb_bot.oms import set_exchange_order_id
from bithumb_bot.recovery import reconcile_with_broker


pytestmark = pytest.mark.slow_integration

CLIENT_ORDER_ID = "flatten_1781881180147"
EXCHANGE_ORDER_ID = "C0101000003112013506"
SELL_QTY = 0.00049913
SELL_PRICE = 95499969.9497125
SELL_FEE = 19.06
INITIAL_CASH = 100000.0
FINAL_CASH = INITIAL_CASH + (SELL_QTY * SELL_PRICE) - SELL_FEE


@pytest.fixture
def live_reconcile_db(managed_runtime_env, monkeypatch):
    db_path = f"{managed_runtime_env['runtime_root']}/data/live/trades/live.sqlite"
    monkeypatch.setenv("DB_PATH", db_path)
    object.__setattr__(settings, "MODE", "live")
    object.__setattr__(settings, "DB_PATH", db_path)
    object.__setattr__(settings, "LIVE_DRY_RUN", False)
    object.__setattr__(settings, "LIVE_REAL_ORDER_ARMED", True)
    object.__setattr__(settings, "PAIR", "KRW-BTC")
    object.__setattr__(settings, "INTERVAL", "1m")
    ensure_db().close()
    runtime_state.enable_trading()
    runtime_state.set_startup_gate_reason(None)
    runtime_state.record_reconcile_result(success=True, reason_code=None, metadata=None, now_epoch_sec=0.0)
    return db_path


class _MissingBaseAccountsCloseoutBroker:
    def __init__(
        self,
        *,
        terminal_sell: bool = True,
        sell_qty: float = SELL_QTY,
        fill_qty: float = SELL_QTY,
        account_cash: float = FINAL_CASH,
    ) -> None:
        self.terminal_sell = bool(terminal_sell)
        self.sell_qty = float(sell_qty)
        self.fill_qty = float(fill_qty)
        self.account_cash = float(account_cash)
        self._accounts_diag: dict[str, object] = {}

    def get_recent_orders(
        self,
        *,
        limit: int = 100,
        exchange_order_ids: list[str] | tuple[str, ...] | None = None,
        client_order_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[BrokerOrder]:
        return []

    def get_open_orders(
        self,
        *,
        exchange_order_ids: list[str] | tuple[str, ...] | None = None,
        client_order_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[BrokerOrder]:
        return []

    def get_order(self, *, client_order_id: str | None = None, exchange_order_id: str | None = None) -> BrokerOrder:
        if client_order_id == CLIENT_ORDER_ID:
            status = "FILLED" if self.terminal_sell else "NEW"
            qty_filled = self.fill_qty if self.terminal_sell else 0.0
            return BrokerOrder(
                str(client_order_id),
                EXCHANGE_ORDER_ID,
                "SELL",
                status,
                SELL_PRICE,
                self.sell_qty,
                qty_filled,
                1,
                2,
            )
        return BrokerOrder(str(client_order_id or "other"), str(exchange_order_id or "ex-other"), "BUY", "NEW", 100.0, 1.0, 0.0, 1, 2)

    def get_fills(
        self,
        *,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        parse_mode: str = "strict",
    ) -> list[BrokerFill]:
        if client_order_id != CLIENT_ORDER_ID or not self.terminal_sell:
            return []
        return [
            BrokerFill(
                client_order_id=CLIENT_ORDER_ID,
                fill_id=f"{EXCHANGE_ORDER_ID}:aggregate:1",
                fill_ts=1781881180999,
                price=SELL_PRICE,
                qty=self.fill_qty,
                fee=SELL_FEE,
                exchange_order_id=EXCHANGE_ORDER_ID,
            )
        ]

    def get_balance_snapshot(
        self,
        *,
        allow_missing_base_for_reconcile: bool = False,
        missing_base_reconcile_reason: str | None = None,
    ) -> BalanceSnapshot:
        source = AccountsV1BalanceSource(
            fetch_accounts_raw=lambda: [
                {"currency": "KRW", "balance": f"{self.account_cash:.8f}", "locked": "0"},
            ],
            order_currency="BTC",
            payment_currency="KRW",
            now_ms=lambda: 1781881182000,
            parse_accounts_response=lambda payload: parse_accounts_response(payload),
            select_pair_balances=lambda accounts, **kwargs: select_pair_balances(accounts, **kwargs),
            to_broker_balance=lambda pair: to_broker_balance(pair),
        )
        snapshot = source.fetch_snapshot(
            allow_missing_base_for_reconcile=allow_missing_base_for_reconcile,
            missing_base_reconcile_reason=missing_base_reconcile_reason,
        )
        self._accounts_diag = source.get_validation_diagnostics()
        return snapshot

    def get_accounts_validation_diagnostics(self) -> dict[str, object]:
        return dict(self._accounts_diag)


def _seed_closeout_order(
    db_path: str,
    *,
    position_qty: float = SELL_QTY,
    order_qty: float = SELL_QTY,
    extra_unresolved_order: bool = False,
) -> None:
    conn = ensure_db(db_path)
    try:
        set_portfolio_breakdown(
            conn,
            cash_available=INITIAL_CASH,
            cash_locked=0.0,
            asset_available=position_qty,
            asset_locked=0.0,
        )
        record_order_if_missing(
            conn,
            client_order_id=CLIENT_ORDER_ID,
            side="SELL",
            qty_req=order_qty,
            price=SELL_PRICE,
            ts_ms=1781881180147,
            status="NEW",
        )
        set_exchange_order_id(CLIENT_ORDER_ID, EXCHANGE_ORDER_ID, conn=conn)
        if extra_unresolved_order:
            record_order_if_missing(
                conn,
                client_order_id="unresolved_local_order",
                side="BUY",
                qty_req=1.0,
                price=100.0,
                ts_ms=1781881180100,
                status="NEW",
            )
            set_exchange_order_id("unresolved_local_order", "ex-unresolved", conn=conn)
        conn.commit()
    finally:
        conn.close()


def test_reconcile_full_sell_closeout_allows_missing_base_row_and_converges(live_reconcile_db):
    _seed_closeout_order(live_reconcile_db)

    reconcile_with_broker(_MissingBaseAccountsCloseoutBroker())

    conn = ensure_db(live_reconcile_db)
    try:
        order = conn.execute(
            "SELECT status, qty_filled FROM orders WHERE client_order_id=?",
            (CLIENT_ORDER_ID,),
        ).fetchone()
        fill_count = conn.execute(
            "SELECT COUNT(*) AS c FROM fills WHERE client_order_id=?",
            (CLIENT_ORDER_ID,),
        ).fetchone()["c"]
        trade_count = conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE client_order_id=?",
            (CLIENT_ORDER_ID,),
        ).fetchone()["c"]
        cash_available, cash_locked, asset_available, asset_locked = get_portfolio_breakdown(conn)
    finally:
        conn.close()

    metadata = json.loads(runtime_state.snapshot().last_reconcile_metadata or "{}")
    assert order is not None
    assert order["status"] == "FILLED"
    assert float(order["qty_filled"]) == pytest.approx(SELL_QTY)
    assert fill_count == 1
    assert trade_count == 1
    assert asset_available == pytest.approx(0.0)
    assert asset_locked == pytest.approx(0.0)
    assert cash_available + cash_locked == pytest.approx(FINAL_CASH)
    assert float(metadata["broker_asset_qty"]) == pytest.approx(0.0)
    assert int(metadata["unresolved_open_order_count"]) == 0
    assert int(metadata["submit_unknown_count"]) == 0
    assert int(metadata["recovery_required_count"]) == 0
    assert int(metadata["missing_base_full_closeout_allowed"]) == 1
    assert metadata["accounts_v1_preflight_outcome"] == "pass_no_position_allowed"


def test_reconcile_missing_base_row_blocks_when_local_unresolved_order_exists(live_reconcile_db):
    _seed_closeout_order(live_reconcile_db, extra_unresolved_order=True)

    with pytest.raises(AccountsRequiredCurrencyMissingError):
        reconcile_with_broker(_MissingBaseAccountsCloseoutBroker())


def test_reconcile_missing_base_row_blocks_without_terminal_closeout_fill(live_reconcile_db):
    _seed_closeout_order(live_reconcile_db)

    with pytest.raises(AccountsRequiredCurrencyMissingError):
        reconcile_with_broker(_MissingBaseAccountsCloseoutBroker(terminal_sell=False))


def test_reconcile_missing_base_row_blocks_when_terminal_sell_qty_does_not_close_position(live_reconcile_db):
    _seed_closeout_order(live_reconcile_db, position_qty=SELL_QTY * 2)

    with pytest.raises(AccountsRequiredCurrencyMissingError):
        reconcile_with_broker(_MissingBaseAccountsCloseoutBroker())


def test_reconcile_missing_base_row_blocks_when_terminal_sell_planned_qty_mismatches_position(live_reconcile_db):
    _seed_closeout_order(live_reconcile_db, order_qty=SELL_QTY / 2)

    with pytest.raises(AccountsRequiredCurrencyMissingError):
        reconcile_with_broker(
            _MissingBaseAccountsCloseoutBroker(
                sell_qty=SELL_QTY / 2,
                fill_qty=SELL_QTY / 2,
            )
        )
