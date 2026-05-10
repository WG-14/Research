from __future__ import annotations

from dataclasses import dataclass


BROKER_TRUTH_ACCOUNTS_V1 = "accounts_v1_rest_snapshot"
BROKER_TRUTH_MYASSET_WS = "myasset_ws_private_stream"
SIMULATION_DRY_RUN_STATIC = "dry_run_static"
PAPER_PORTFOLIO_BALANCE = "paper_portfolio"
LIVE_DRY_RUN_BROKER_TRUTH_SOURCE_VIOLATION = "LIVE_DRY_RUN_BROKER_TRUTH_SOURCE_VIOLATION"


@dataclass(frozen=True)
class BalanceAuthorityMatrix:
    broker_cash_truth: str
    broker_position_truth: str
    resume_recovery_safety_gate: str
    order_sizing: str
    simulation_balance: str
    paper_portfolio_balance: str
    balance_authority: str
    simulation_balance_source: str
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "broker_cash_truth": self.broker_cash_truth,
            "broker_position_truth": self.broker_position_truth,
            "resume_recovery_safety_gate": self.resume_recovery_safety_gate,
            "order_sizing": self.order_sizing,
            "simulation_balance": self.simulation_balance,
            "paper_portfolio_balance": self.paper_portfolio_balance,
            "balance_authority": self.balance_authority,
            "simulation_balance_source": self.simulation_balance_source,
            "notes": list(self.notes),
        }


def resolve_balance_authority_matrix(
    *,
    mode: str,
    live_dry_run: bool,
    live_real_order_armed: bool,
    myasset_ws_enabled: bool = False,
) -> BalanceAuthorityMatrix:
    normalized_mode = str(mode or "").strip().lower() or "paper"
    if normalized_mode != "live":
        return BalanceAuthorityMatrix(
            broker_cash_truth=PAPER_PORTFOLIO_BALANCE,
            broker_position_truth=PAPER_PORTFOLIO_BALANCE,
            resume_recovery_safety_gate=PAPER_PORTFOLIO_BALANCE,
            order_sizing=PAPER_PORTFOLIO_BALANCE,
            simulation_balance=PAPER_PORTFOLIO_BALANCE,
            paper_portfolio_balance=PAPER_PORTFOLIO_BALANCE,
            balance_authority=PAPER_PORTFOLIO_BALANCE,
            simulation_balance_source=PAPER_PORTFOLIO_BALANCE,
            notes=("paper_mode_uses_local_portfolio",),
        )

    if bool(live_dry_run) and not bool(live_real_order_armed):
        return BalanceAuthorityMatrix(
            broker_cash_truth=BROKER_TRUTH_ACCOUNTS_V1,
            broker_position_truth=BROKER_TRUTH_ACCOUNTS_V1,
            resume_recovery_safety_gate=BROKER_TRUTH_ACCOUNTS_V1,
            order_sizing=BROKER_TRUTH_ACCOUNTS_V1,
            simulation_balance=SIMULATION_DRY_RUN_STATIC,
            paper_portfolio_balance=PAPER_PORTFOLIO_BALANCE,
            balance_authority=BROKER_TRUTH_ACCOUNTS_V1,
            simulation_balance_source=SIMULATION_DRY_RUN_STATIC,
            notes=("live_dry_run_static_cash_is_simulation_only",),
        )

    broker_truth = BROKER_TRUTH_MYASSET_WS if bool(myasset_ws_enabled) else BROKER_TRUTH_ACCOUNTS_V1
    return BalanceAuthorityMatrix(
        broker_cash_truth=broker_truth,
        broker_position_truth=broker_truth,
        resume_recovery_safety_gate=broker_truth,
        order_sizing=broker_truth,
        simulation_balance="none",
        paper_portfolio_balance=PAPER_PORTFOLIO_BALANCE,
        balance_authority=broker_truth,
        simulation_balance_source="none",
        notes=("live_real_order_path_uses_private_account_truth",),
    )


def is_unarmed_live_dry_run(*, mode: str, live_dry_run: bool, live_real_order_armed: bool) -> bool:
    return (
        str(mode or "").strip().lower() == "live"
        and bool(live_dry_run)
        and not bool(live_real_order_armed)
    )


def live_dry_run_broker_truth_source_violation(
    *,
    mode: str,
    live_dry_run: bool,
    live_real_order_armed: bool,
    candidate_source_id: str,
) -> dict[str, object] | None:
    if not is_unarmed_live_dry_run(
        mode=mode,
        live_dry_run=live_dry_run,
        live_real_order_armed=live_real_order_armed,
    ):
        return None
    got = str(candidate_source_id or "").strip() or "unknown"
    if got != SIMULATION_DRY_RUN_STATIC:
        return None
    return {
        "balance_authority_violation": LIVE_DRY_RUN_BROKER_TRUTH_SOURCE_VIOLATION,
        "balance_authority_violation_expected": BROKER_TRUTH_ACCOUNTS_V1,
        "balance_authority_violation_got": got,
        "expected": BROKER_TRUTH_ACCOUNTS_V1,
        "got": got,
        "simulation_balance_source": SIMULATION_DRY_RUN_STATIC,
    }
