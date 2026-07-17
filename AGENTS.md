# AGENTS.md

## Purpose

This is the `market-research` platform monorepo. It contains three deliberately
separate distributions:

- `market-research`, the offline reproducible research engine and CLI;
- `apps/internal_web`, the authenticated internal-web adapter; and
- `services/research_operations`, the operational trust domain for that offline
  service.

The platform uses externally prepared immutable market datasets, runs backtests
and walk-forward studies, performs statistical validation, and writes research
reports and audit evidence. It is not a trading bot.

## Repository boundaries

Operational code is permitted only under `services/research_operations` and
its deployment support paths. The `src/market_research` distribution must not
import Django, `market_research_web`, `portal`, or `research_operations`, and it
must not own PostgreSQL queues, service supervision, TLS, health checks, or
operational backup/restore. Web and Operations may depend only on published
Research application contracts or explicit adapter boundaries; Research must
never depend on either adapter.

`/home/vorac/work/Operation` is a separate trading-system repository. It must
never be imported, modified, copied into, or used by this platform.

Network market-data collection, operational order/fill database ingestion,
exchange raw order-semantics inference, and retry/backfill/source-probe
workflows are forbidden. Inputs are externally prepared immutable datasets and
canonical research artifacts only.

These boundaries correspond to the architecture domains
`network_market_data_collection`, `operational_order_fill_database_ingestion`,
`exchange_raw_order_semantics_inference`, and `retry_backfill_source_probe`.

## Supported strategies

The supported strategy set is exactly:

- `sma_with_filter`
- `buy_and_hold_baseline`
- `noop_baseline`
- `threshold_research_only`

Do not change their signal, trade, price, fee, or performance semantics unless
the task explicitly changes a reviewed research contract.

## Research artifact and path rules

All datasets, artifacts, reports, cache entries, and SQLite files are absolute
repository-external paths. Use `ResearchSettings` and `ResearchPathManager`
for every path. Datasets are immutable inputs; derived artifacts and reports
use atomic writes, and audit streams use append-only JSONL.

Artifact evidence must retain manifest, dataset, parameter, execution
assumption, seed, and content-hash bindings needed to reproduce a study.

## Research integrity priorities

### P0

- reproducible research results
- dataset, manifest, and artifact-hash integrity
- no look-ahead bias
- separated train, validation, and final-holdout use
- preserved fee and slippage assumptions
- deterministic strategy behavior
- repository-external artifacts

### P1

- statistical validation and walk-forward studies
- audit trail
- deterministic resource limits and parallel execution
- clear research diagnostics

### P2

- execution speed
- development convenience
- additional strategies

## Manifest and schema rules

Preserve Research Semantics v2. Reject unknown legacy manifest fields rather
than silently translating them. Keep manifests and artifacts explicit about
their schema version, evidence scope, and hash bindings.

## Testing policy

Run focused tests directly related to a patch. For repository-wide cleanup or
an explicitly requested validation task, run focused tests first, then
collection, then one full pytest invocation. After that invocation, rerun only
the reported failures with focused selectors.

## Codex execution policy

Default patches use focused validation. Repository-wide cleanup and explicit
integration validation may use one approved full suite after focused checks
pass. Do not replace diagnosis with repeated broad test runs.

## Operations service boundary

`services/research_operations` may own PostgreSQL coordination, durable worker
leases and fencing, audit projection, health/readiness, deployment, and
backup/recovery for the offline research service. It must keep runtime state,
credentials, certificates, datasets, artifacts, reports, and backups outside
Git and outside the source tree.

Across the entire monorepo, do not add account-connected trading, private
exchange APIs, order submission or management, account access, operational
order/fill ingestion, reviewed-account profiles, runtime trading strategies,
emergency account controls, or real-account environment variables.
