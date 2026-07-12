# Changelog

## Unreleased

- Removed the legacy exchange package/API surface and completed the breaking
  rename to `market-research` / `market_research`.
- Removed operational execution-log analysis and raw exchange order semantics.
- Removed retry, source-probe, and persistent-missing-candle artifact contracts.
- Enforced the offline boundary against network code, remote-data names,
  operational database tables, and network runtime dependencies.

### 0.1.0

- research-only repository boundary
- Research Semantics v2
- four supported strategies
- deterministic backtest and walk-forward
- artifact-backed research validation
- strict fail-closed reproduction fingerprint and drift verification
- wheel and sdist isolated-install validation
- distribution checksums
- `market_research` package and CLI
