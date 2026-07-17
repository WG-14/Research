# Internal Web Implementation Record

This record summarizes the web adapter's architectural progression. It records
source-level outcomes and gates; release-specific test results belong in an
immutable acceptance record, not in this living document.

## Iteration 1 — research boundary inventory

The existing CLI, path manager, artifact formats, governance registries, and
run lifecycle were treated as the research contract. HTTP handlers were not
permitted to wrap arbitrary CLI arguments or expose raw report payloads.

Outcome:

- Research Semantics v2 and the four-strategy catalog stayed unchanged.
- Every CLI capability received a GUI policy.
- Repository-external paths and immutable artifact authority stayed intact.

## Iteration 2 — UI-neutral application contracts

Framework-neutral request, result, capability, actor, and presenter contracts
were placed under `market_research.application`. The CLI and web adapter share
those contracts without calling one another.

Outcome:

- application services independently enforce actor permissions;
- web-safe projections omit paths and infrastructure details;
- dependency tests keep Django out of the Research distribution.

## Iteration 3 — isolated Django adapter

The server-rendered adapter was created in `apps/internal_web` with sessions,
CSRF protection, explicit RBAC, bounded forms, object visibility, and separate
packaging. A generic shell/CLI surface was intentionally excluded.

Outcome:

- authenticated list/detail workflows use opaque IDs;
- state changes create auditable, correlated events;
- login throttling, secure production settings, and step-up confirmation have
  explicit contracts.

## Iteration 4 — durable job and governance workflows

Job requests became durable database records with immutable request material,
idempotency, one-active-job constraints, bounded workload admission, and safe
terminal projections. Human review and approval added separation of duties,
locked evidence checks, unresolved-requirement handling, and replay safety.

Outcome:

- long-running execution is outside HTTP requests;
- automated validation does not grant human approval;
- stale leases cannot publish without the current fencing material.

## Iteration 5 — audit and historical report boundaries

ORM mutations and immutable audit intents were joined in database
transactions, with external JSONL projection handled as a detectable,
idempotent second authority. Historical report import was restricted to an
administrator-only, allowlisted, bounded, no-follow ingestion workflow.

Outcome:

- audit lag, duplicate, orphan, and hash mismatch states are distinguishable;
- imported reports become managed, content-addressed objects;
- original server paths are neither persisted nor returned.

## Iteration 6 — PostgreSQL operational coordination

The `research-operations` distribution owns durable outbox delivery,
experiment admission, worker leases and fencing, job-result receipts, health,
backup fences, and recovery evidence. The web adapter exports the explicit
`market_research_web.operations_contract` facade used by Operations.

Outcome:

- SQLite remains a local development profile only;
- production concurrency claims require PostgreSQL-specific tests with no
  skips;
- an unapplied result receipt or unhealthy audit projection blocks readiness.

## Iteration 7 — monorepo consolidation

Research, Web, and Operations were placed in one Git history and one `uv`
workspace. All packages share the root lock and build from the same commit.
Direct cross-package implementation imports were replaced by explicit public
facades.

Outcome:

- a single checkout contains code, migrations, tests, deployment assets, and
  documentation for the complete platform;
- package and static architecture tests enforce dependency direction;
- runtime data, secrets, certificates, and backups remain outside Git.

## Iteration 8 — release identity and operated admission

The deterministic release manifest binds the Git SHA, three distribution
versions and artifacts, unified lock, migration sets, deployment assets, and
aggregate digests. Operations persists worker release provenance and checks it
in readiness. The official service profile disables direct Research CLI use.

Outcome:

- mixed-version workers fail closed instead of silently processing jobs;
- the operated host admits execution only through the Operations service;
- local offline CLI behavior remains available outside the operated profile.

## Iteration 9 — official native deployment contract

One official profile was selected:
`services/research_operations/deploy/native`. It defines systemd supervision,
Nginx/Gunicorn boundaries, PostgreSQL role separation, fail-closed preflight,
scheduled backup, retention audit, and application health/readiness. Compose is
kept only as a non-official portability reference.

Outcome:

- process restart and SIGTERM limits are explicit in unit files;
- private material and mutable state use external host paths;
- preflight requires release, owner, PKI, storage, off-site, retention, RPO,
  and RTO inputs.

## Iteration 10 — release acceptance boundary

The repository defines focused, package, integration, browser, migration,
backup/recovery, and deployment checks. It deliberately does not convert test
fixtures or example configuration into site approval.

Completion requires release-specific evidence for the clean checkout and
actual target host. The following remain external until an organization
provides them:

- stable named owners and on-call/incident routing;
- organization-managed PKI with renewal, revocation, and expiry monitoring;
- an encrypted off-site export hook and independently verified destination;
- approved retention/legal-hold rules and measured RPO/RTO;
- alert integration and scheduled restore/rollback drills.

## Current invariant summary

- The platform performs offline research only.
- Research never depends on Web or Operations.
- Web and Operations use published adapter contracts.
- All persistent state and private material remain outside the source tree.
- Production execution is PostgreSQL-coordinated and admitted; direct CLI is
  blocked in the operated profile.
- Native systemd is official; Compose is non-official.
- A repository-level passing test is necessary but insufficient for a
  production-readiness claim.
