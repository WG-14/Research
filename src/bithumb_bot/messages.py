from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OperatorMessage:
    reason_code: str
    severity: str
    message: str
    recommended_action: str
    docs_hint: str = ""

    def format(self, prefix: str, **fields: object) -> str:
        details = " ".join(
            f"{key}={value}"
            for key, value in fields.items()
            if value is not None and str(value).strip() != ""
        )
        base = (
            f"[{prefix}] {self.message} reason_code={self.reason_code} "
            f"severity={self.severity} action={self.recommended_action}"
        )
        if self.docs_hint:
            base = f"{base} docs={self.docs_hint}"
        if details:
            base = f"{base} {details}"
        return base


LIVE_DB_PATH_REQUIRED = OperatorMessage(
    reason_code="LIVE_DB_PATH_REQUIRED",
    severity="CRITICAL",
    message="DB_PATH must be explicitly set when MODE=live.",
    recommended_action="set_DB_PATH_to_repository_external_live_trades_sqlite",
    docs_hint="docs/storage-layout.md#allowed-overrides",
)

ACCOUNTS_PREFLIGHT_AUTH_FAILED = OperatorMessage(
    reason_code="ACCOUNTS_AUTH_FAILED",
    severity="CRITICAL",
    message="/v1/accounts REST snapshot preflight authentication failed.",
    recommended_action="verify_api_key_secret_permissions_and_clock_before_live_start",
)

ACCOUNTS_PREFLIGHT_TRANSPORT_FAILED = OperatorMessage(
    reason_code="ACCOUNTS_TRANSPORT_FAILED",
    severity="CRITICAL",
    message="/v1/accounts REST snapshot preflight transport failed.",
    recommended_action="verify_network_dns_tls_and_bithumb_api_reachability_before_live_start",
)

SIGNAL_INSUFFICIENT_CANDLES = OperatorMessage(
    reason_code="SIGNAL_INSUFFICIENT_CANDLES",
    severity="WARN",
    message="Not enough closed candle data to compute the diagnostic signal.",
    recommended_action="run_sync_then_retry_signal",
)

EXPLAIN_INSUFFICIENT_CANDLES = OperatorMessage(
    reason_code="EXPLAIN_INSUFFICIENT_CANDLES",
    severity="WARN",
    message="Not enough closed candle data to explain the diagnostic signal.",
    recommended_action="run_sync_then_retry_explain",
)

STATUS_MISSING_CANDLE_DATA = OperatorMessage(
    reason_code="STATUS_MISSING_CANDLE_DATA",
    severity="WARN",
    message="No candle data is available for status valuation.",
    recommended_action="run_sync_before_status",
)
