# AGENTS.md

## Purpose

`bithumb-research` is an offline, reproducible market-strategy research
repository. It prepares public-market datasets, runs backtests and
walk-forward studies, performs statistical validation, and writes research
reports and audit evidence. It is not a trading bot.

## Repository boundaries

The repository contains research code, tests, documentation, examples, and
development validation scripts. It must not contain account access, private
account APIs, order submission, account-connected runtime, state
repair, single-instance coordination, service management, deployment, or
operator tooling.

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

## Forbidden operational functionality

Do not add account-connected trading, private APIs, order submission, order
management, account access, state repair, single-instance coordination, service
units, health checks, operational backup/restore, reviewed-account profiles,
runtime strategies, operator commands, emergency account controls, or
real-account environment variables.
