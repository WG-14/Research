# Market Research Platform

This repository is the Git monorepo for an offline, reproducible market
research platform. It contains the research engine, its authenticated internal
web adapter, and the operations trust domain needed to run that web adapter.
It is not a trading bot: account access, private exchange APIs, order or fill
ingestion, order submission, and runtime trading controls are outside scope.

## Distributions

| Distribution | Source | Responsibility |
| --- | --- | --- |
| `market-research` | `src/market_research` | Framework-neutral research engine, deterministic CLI, artifacts, governance, and public application contracts. |
| `market-research-internal-web` | `apps/internal_web` | Django authentication, RBAC, CSRF protection, browser workflows, safe projections, and web metadata. |
| `research-operations` | `services/research_operations` | PostgreSQL coordination, durable workers, health/readiness, audit projection, release admission, backup/recovery, and deployment assets. |

All three packages share the root `uv.lock`. The dependency direction is
strictly one way:

```text
research-operations
  -> market_research_web.operations_contract
  -> market_research.application / adapter_contracts

market-research-internal-web
  -> market_research.application / adapter_contracts

market-research
  -X-> web or operations packages
```

The web package does not import Research implementation modules directly, and
Operations reaches web behavior only through
`market_research_web.operations_contract`. See
[`docs/monorepo-architecture.md`](docs/monorepo-architecture.md) for the full
boundary.

## Workspace commands

Python 3.12 and `uv` are the supported workspace baseline.

```sh
scripts/platform bootstrap
scripts/platform test-core
scripts/platform test-web
scripts/platform test-operations
scripts/platform test-all
scripts/platform test-browser
scripts/platform test-integration
scripts/platform lint
scripts/platform typecheck
scripts/platform compile
scripts/platform docs-check
scripts/platform verify-complete --help
scripts/platform audit
scripts/platform build
scripts/platform install-release --help
scripts/platform verify-deployment
scripts/platform backup-restore-drill --help
scripts/platform research --help
```

`bootstrap` performs a frozen install of every package and dependency group
from the root lock. The package-specific test commands remain available when a
change affects only one trust domain.

`verify-complete` evaluates the strict 153-criterion receipt manifest. Its
opt-in `--run-evidence` mode writes only to a new absolute repository-external
root; see
[`docs/platform-completeness-evidence-runner.md`](docs/platform-completeness-evidence-runner.md).

## Research CLI

For a researcher-controlled offline workstation, the canonical command is the
deterministic workspace wrapper:

```sh
scripts/platform research <command>
```

It fixes Python hash seeding and all six supported numerical backend thread
counts before Python starts; strict receipts independently verify those values.

The supported strategy set is exactly:

- `sma_with_filter`
- `buy_and_hold_baseline`
- `noop_baseline`
- `threshold_research_only`

Each strategy is a hash-bound package with a strict sidecar manifest, complete
parameter and hypothesis metadata, automatic failure-isolated discovery, and a
common decision/result contract. See
[`docs/strategy-development.md`](docs/strategy-development.md) for the
add/validate/approve/retire workflow. Multi-manifest jobs use a network-denied,
read-only Linux process sandbox; operated jobs execute in supervised child
processes so a strategy timeout or memory failure does not take down the
control plane.

The CLI consumes externally prepared immutable datasets. A typical local
workflow is:

```sh
scripts/platform research research-freeze-dataset \
  --db /abs/candles.sqlite \
  --market KRW-BTC --interval 1m --start 2025-01-01 --end 2025-03-31 \
  --provenance-manifest /abs/dataset-source-provenance.json \
  --out /abs/datasets
scripts/platform research research-readiness \
  --manifest /abs/experiment.json --json
scripts/platform research research-backtest \
  --manifest /abs/experiment.json
scripts/platform research research-walk-forward \
  --manifest /abs/experiment.json
scripts/platform research research-validate \
  --manifest /abs/experiment.json
```

Replay an authoritative receipt with the same deterministic launcher:

```sh
scripts/platform research research-reproduce-run \
  --manifest /abs/experiment.json \
  --receipt /abs/reports/experiment-id/reproduction-receipt.json \
  --out /abs/reports/reproduction-result.json
```

The command exits zero only for `status=PASS`; drift and invalid baselines exit
nonzero. The comparison document is written to `--out` (or beneath the
configured external report root when omitted), and records the isolated
reproduced report and receipt paths plus exact drift rows.

The freeze command prints the generated schema-3 `artifact_manifest_uri` and
`artifact_manifest_hash`. Bind both exact values into the experiment manifest
and set `dataset.source=frozen_sqlite_candles`. The mutable
`dataset.source=sqlite_candles` compatibility path is exploratory only and
cannot produce an authoritative reproduction receipt.

On an operated service host, `RESEARCH_RUNTIME_PROFILE=operated` disables the
direct `market-research` entrypoint with a fail-closed exit. Jobs must enter
through the authorized Operations admission and fencing path. This gate
prevents the installed service profile from bypassing operational admission;
it does not turn research output into trading permission.

## External state

No runtime state belongs in this checkout. The following settings must resolve
to absolute repository-external locations:

- `RESEARCH_DATA_ROOT`: immutable or externally prepared datasets;
- `RESEARCH_ARTIFACT_ROOT`: derived artifacts and managed static output;
- `RESEARCH_REPORT_ROOT`: research and operator-readable reports;
- `RESEARCH_CACHE_ROOT`: disposable cache;
- `RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH`: append-only experiment identity authority;
- `RESEARCH_DB_PATH`: optional local SQLite candle input for commands that require it.

Production PostgreSQL, credentials, certificates, private keys, backups,
off-site receipts, restored namespaces, and logs also remain outside Git.
`ResearchSettings` and `ResearchPathManager` are the canonical Research path
boundary. Datasets are immutable inputs; derived files use atomic publication,
and evidence streams use append-only hash chains.

## Workspace build and release identity

Build all three distributions from one clean commit:

```sh
scripts/platform build
scripts/platform release-manifest \
  --release-id platform-YYYY.MM.DD.N \
  --artifacts-dir "$PWD/dist/platform" \
  --output /absolute/release-staging/release.json
```

The build command and generator refuse a dirty checkout. `build` creates a
temporary `git archive` of `HEAD`, injects canonical build provenance, and
builds from that immutable snapshot rather than from the developer working
tree. The generator opens every archive and rejects it unless its package
metadata, complete package payload, and embedded provenance match that exact
checkout. It binds:

- the 40-character Git SHA and release ID;
- all three distribution names and versions;
- every wheel and sdist filename, size, and SHA-256 digest;
- the unified `uv.lock` digest;
- Django and Operations migration counts, latest revisions, and digests;
- the official native-deployment digest;
- aggregate build and release-bundle digests.

Every wheel and sdist contains a canonical `_build_provenance.json` with its
distribution/version, Git SHA, component source digest, and a shared platform
source digest. This prevents artifacts from two commits that happen to use the
same `0.1.0` package version from being combined. A filename with arbitrary or
well-formed-but-different bytes is not release evidence.

The promoted `release.json` must be root-owned, immutable to the service user,
and match `RESEARCH_OPS_GIT_SHA`, `RESEARCH_OPS_RELEASE_ID`, and
`RESEARCH_OPS_BUILD_DIGEST`. Worker heartbeats and readiness checks reject a
mixed or missing release identity.

## Deployment status

The only official deployment profile is
[`services/research_operations/deploy/native`](services/research_operations/deploy/native):
PostgreSQL 16, Nginx, Gunicorn, and systemd on one qualified Linux host.
`services/research_operations/deploy/compose.yaml` is a non-official portability
reference and is not deployment acceptance evidence.

The checked-in profile implements fail-closed preflight, service supervision,
durable workers, health/readiness, backup fencing, signed backup metadata,
blank-namespace recovery verification, dry-run retention auditing, and an
encrypted off-site export hook contract. A site must still provide and approve
all external operating inputs before promotion:

- named service, security, data, on-call, incident, backup, and recovery owners;
- organization-issued server, database, and operations-client PKI plus renewal and revocation procedures;
- an independently installed encrypted off-site export implementation and destination;
- approved retention, legal-hold, RPO, and RTO policy;
- alert routing, scheduled restore drills, host/storage qualification, and release-specific acceptance evidence.

Repository tests and example preflight do not satisfy those organization-owned
gates. Do not describe a release as production-ready until the release
checklist and site runbook have evidence for the actual host and release.

## Further documentation

- [`docs/monorepo-architecture.md`](docs/monorepo-architecture.md): trust domains, authorities, and dependency rules
- [`docs/internal-web-architecture.md`](docs/internal-web-architecture.md): web capabilities and security contract
- [`docs/internal-web-operations-handoff.md`](docs/internal-web-operations-handoff.md): operator ownership and runbook handoff
- [`docs/research-data-dictionary.md`](docs/research-data-dictionary.md): generated canonical dataset field semantics and ownership
- [`docs/strategy-development.md`](docs/strategy-development.md): strategy package authoring, validation, isolation, and retirement
- [`docs/monorepo-iterations.md`](docs/monorepo-iterations.md): consolidation record and remaining gates
- [`docs/release-checklist.md`](docs/release-checklist.md): release and promotion evidence checklist
- [`services/research_operations/deploy/native/README.md`](services/research_operations/deploy/native/README.md): official deployment procedure
- [`services/research_operations/docs/runbook.md`](services/research_operations/docs/runbook.md): backup, recovery, and incident procedures
