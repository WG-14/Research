# Monorepo Architecture

## Platform shape

The repository is one versioned release unit containing three separately
packaged trust domains:

```text
Research (offline semantics and artifacts)
  ^
  | public application and adapter contracts
  |
Web (identity, RBAC, browser workflows, managed projections)
  ^
  | market_research_web.operations_contract
  |
Operations (PostgreSQL coordination, workers, health, release, recovery)
```

The arrows point toward dependencies. Reverse imports are prohibited. The root
`uv.lock` and root workspace install/build commands provide one reproducible
dependency graph without merging the three distributions' responsibilities.

## Research distribution

`market-research` owns deterministic study semantics, immutable dataset and
manifest validation, strategy composition, backtest/walk-forward/statistical
validation, governance evidence, reports, and the offline CLI. It exposes
framework-neutral contracts under `market_research.application`.

`market_research.application.platform_contracts` is the narrow shared facade
for repository-external path settings and crash-safe publication primitives.
Web and Operations do not import `market_research.paths`,
`market_research.settings`, or `market_research.storage_io` directly. The
module paths in `architecture-boundaries.json` are executable exact
allowlists. A listed package module does not authorize its descendants; the
architecture tests reject both undeclared imports and stale allowlist entries.

It does not own:

- Django, sessions, browser authorization, or HTTP views;
- PostgreSQL queues, service leases, health endpoints, or supervision;
- TLS ingress, service installation, backup, restore, or on-call tooling;
- exchange connectivity, accounts, orders, fills, or live trading.

## Web distribution

`market-research-internal-web` owns the authenticated human interface. Django
provides sessions, CSRF protection, RBAC, migrations, forms, safe projections,
and web metadata. Views invoke typed application services instead of CLI
parsers or subprocesses.

`market_research_web.operations_contract` is the only supported Operations-to-
portal facade. Expanding it is a cross-distribution compatibility decision.

## Operations distribution

`research-operations` owns the runtime trust boundary around the offline
service:

- PostgreSQL outbox delivery and dead-letter state;
- experiment identity admission, durable leases, and fencing;
- persistent research job workers and terminal result receipts;
- worker release provenance and workflow readiness;
- metrics and path/secret-redacted diagnostics;
- migration/ACL orchestration, backup fencing, signed manifests, restore
  verification, and explicit activation;
- native systemd/Nginx/Gunicorn deployment assets.

Operations may coordinate execution but may not change Research semantics or
publish a result after losing its fence.

## Data and transaction boundaries

There are multiple explicit authorities:

| Authority | Content |
| --- | --- |
| Immutable external filesystem | datasets, manifests, research artifacts, reports, managed imported reports |
| Append-only external streams | governance, experiment identity, audit evidence |
| Django PostgreSQL schema | users, permissions, web jobs, audit intents, managed catalog |
| Operations PostgreSQL schema | outbox delivery, admission, worker heartbeats, backup fence, receipts, restore evidence |
| Git release manifest | code/build/migration/deployment identity |

No design pretends that PostgreSQL and filesystem publication are one atomic
transaction. Hash bindings, idempotent projections, admission fences, result
receipts, validation observations, and readiness expose and control the
boundary.

## Runtime profiles

The local offline profile permits the Research CLI and may use a researcher-
controlled SQLite input where a command supports it. The operated profile uses
PostgreSQL and sets `RESEARCH_RUNTIME_PROFILE=operated`, which disables direct
CLI execution. All service-host work enters through authorized Operations
admission.

The distinction is intentional:

- local CLI compatibility is preserved for offline research;
- the operated host cannot bypass durable identity, permission, admission,
  lease, fencing, and audit controls through the public CLI entrypoint.

## Release unit

`tools/build_release_artifacts.py` first creates a temporary `git archive` of
the clean `HEAD`, embeds canonical per-distribution provenance, and builds all
six artifacts from that snapshot. `tools/release_manifest.py` then opens each
wheel and sdist and proves its metadata, complete package payload, embedded
Git SHA, component source digest, and shared platform source digest against
the checkout before producing the canonical release identity. Its manifest
binds:

- Git SHA and release ID;
- names and versions of Research, Web, and Operations;
- wheel/sdist hashes and sizes;
- root lock digest;
- Django and Operations migration sets;
- official native deployment digest;
- aggregate build and release-bundle digests.

The embedded SHA and shared platform digest distinguish same-version packages
from different commits. The official installer accepts only the three exact
manifest-bound wheels and the installed-release verifier rejects editable,
directory, sdist-derived, mixed-commit, or locally modified installations.

The release ID, SHA, and build digest are carried by all supervised processes.
Workers persist them in heartbeats. Readiness rejects incompatible worker
provenance so the system cannot silently process with mixed binaries.

## Deployment decision

The single official model is native systemd on one qualified Linux host:

- PostgreSQL 16 is the supported coordination database;
- Nginx terminates employee TLS and protects a separate mTLS operations
  listener;
- Gunicorn hosts web and diagnostics applications;
- systemd supervises web, API, outbox workers, job worker, validator, preflight,
  backup, and retention audit.

Compose is a non-official portability reference. Changing the official model
requires an explicit architecture decision, target-host acceptance, and an
updated `deploy/OFFICIAL_DEPLOYMENT` marker in the same reviewed release.

## Runtime-state exclusion

Only source, migrations, examples, tests, and deployment templates belong in
Git. The following never do:

- production environment files, passwords, database URLs, private keys,
  certificates, htpasswd, or signing material;
- datasets, SQLite/PostgreSQL data, artifacts, reports, caches, audit streams,
  backups, restores, off-site receipts, or logs;
- mutable service sockets, PIDs, temporary files, or generated PKI.

The repository `.gitignore`, Docker build context, architecture tests, release
inspection, and preflight provide layered enforcement. None replaces an
organization's secret scanner or host policy.

## Security and research non-goals

Across every distribution, the following remain forbidden: private exchange
access, account connection, order submission/management, operational order or
fill ingestion, raw exchange semantics inference, source-probe/backfill
automation, runtime trading strategies, reviewed-account profiles, and
emergency account controls.

Research output is evidence for human review. It never grants trading or
account authority.

## Acceptance boundary

Repository validation can prove import direction, deterministic packaging,
test behavior, migration shape, service-unit syntax, and example fail-closed
configuration. A concrete promotion additionally requires externally supplied
owner assignments, organization PKI, secret distribution, alert routing,
encrypted off-site storage, approved retention/legal hold and RPO/RTO, and
release-specific upgrade/rollback and restore evidence.

Those external inputs are promotion gates, not optional enhancements.
