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

CONFIG_LINT_MESSAGES = {
    "approved_profile_placeholder": OperatorMessage(
        reason_code="APPROVED_PROFILE_PLACEHOLDER",
        severity="ERROR",
        message="Live approved profile selector is still a placeholder.",
        recommended_action="set_verified_approved_strategy_profile_path",
        docs_hint="README.md#live-safety",
    ),
    "approved_profile_not_configured": OperatorMessage(
        reason_code="APPROVED_PROFILE_NOT_CONFIGURED",
        severity="ERROR",
        message="Live approved profile selector is not configured.",
        recommended_action="set_verified_approved_strategy_profile_path",
        docs_hint="README.md#live-safety",
    ),
    "deprecated_ignored_env_key": OperatorMessage(
        reason_code="DEPRECATED_IGNORED_ENV_KEY",
        severity="WARN",
        message="Deprecated ignored environment key is present.",
        recommended_action="remove_deprecated_ignored_env_key",
        docs_hint="docs/config-reference.md",
    ),
    "secret_bearing_key_has_surrounding_whitespace": OperatorMessage(
        reason_code="SECRET_VALUE_SURROUNDING_WHITESPACE",
        severity="ERROR",
        message="Secret-bearing environment value has surrounding whitespace.",
        recommended_action="trim_secret_value_in_runtime_env_file",
        docs_hint="docs/config-reference.md",
    ),
    "bithumb_api_secret_too_short": OperatorMessage(
        reason_code="AUTH_SECRET_TOO_SHORT",
        severity="ERROR",
        message="Bithumb API secret is too short for HS256 JWT signing.",
        recommended_action="replace_BITHUMB_API_SECRET_with_32_plus_byte_hs256_secret",
        docs_hint="docs/config-reference.md",
    ),
    "paper_only_key_in_live_env": OperatorMessage(
        reason_code="PAPER_ONLY_KEY_IN_LIVE_ENV",
        severity="WARN",
        message="Paper-only environment key is present in live mode.",
        recommended_action="remove_paper_only_key_from_live_env_file",
        docs_hint="docs/storage-layout.md",
    ),
    "live_env_file_source_mismatch": OperatorMessage(
        reason_code="LIVE_ENV_FILE_SOURCE_MISMATCH",
        severity="ERROR",
        message="Explicit env file selector differs from live env file selector.",
        recommended_action="align_BITHUMB_ENV_FILE_and_BITHUMB_ENV_FILE_LIVE",
        docs_hint="README.md#env-loading-rules",
    ),
    "risky_live_limit": OperatorMessage(
        reason_code="RISKY_LIVE_LIMIT",
        severity="WARN",
        message="Live risk limit is outside the conservative operating envelope.",
        recommended_action="review_and_reduce_live_risk_limit_or_document_operator_approval",
        docs_hint="README.md#live-safety",
    ),
    "unknown_bot_related_env_key": OperatorMessage(
        reason_code="UNKNOWN_BOT_RELATED_ENV_KEY",
        severity="WARN",
        message="Bot-related environment key is not declared in ConfigSpec.",
        recommended_action="remove_unknown_key_or_add_it_to_ConfigSpec_with_tests",
        docs_hint="docs/config-reference.md",
    ),
}
