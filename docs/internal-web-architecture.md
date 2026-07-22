# Internal Web Architecture Contract

## Status and scope

The internal web GUI is the authenticated adapter in
`apps/internal_web`. Its operational trust domain is embedded in this monorepo
at `services/research_operations`. The web adapter does not change Research
Semantics v2, become an authority for research artifacts, or authorize any
trading activity.

This document records implemented source contracts. It is not evidence that a
particular release, host, database, PKI, backup destination, or on-call process
has passed site acceptance.

## Trust domains and dependency direction

```text
browser
  -> Nginx / TLS
  -> Django web adapter
       -> market_research.application
       -> market_research.application.adapter_contracts

systemd-supervised Operations processes
  -> market_research_web.operations_contract
  -> market_research.application / adapter_contracts
  -> PostgreSQL coordination and external evidence roots
```

The following rules are mandatory:

1. `market-research` is framework-neutral and never imports Django, `portal`,
   `market_research_web`, or `research_operations`.
2. The web adapter reaches Research through published application or
   composition contracts, never through Research CLI implementation modules.
3. Operations reaches portal behavior only through
   `market_research_web.operations_contract` and reaches Research only through
   public application contracts.
4. Web and Operations may adapt Research; Research never depends on either.
5. All three distributions are built from the same Git commit and root lock.

Static architecture tests enforce these import directions. A facade is a
review boundary, not permission to export arbitrary implementation details.

## Authorities and storage

Research artifacts remain canonical for deterministic study evidence.
PostgreSQL is authoritative for authenticated identities, permissions, durable
jobs, leases and fencing tokens, web metadata, audit intents, admission state,
worker heartbeats, and recovery coordination. Append-only external registries
remain authoritative for experiment identity and research governance.

PostgreSQL and filesystems cannot share one physical transaction. The platform
therefore uses explicit receipts, content hashes, idempotent projection, and
readiness checks at that boundary. A successful database commit is not by
itself proof that an external artifact was published, and an orphan filesystem
write is not an admitted result.

All datasets, registries, artifacts, reports, caches, databases, backups,
certificates, credentials, and restored namespaces use absolute paths outside
the checkout. Browser requests normally carry opaque IDs, never server paths.
The narrowly scoped historical report importer is administrator-only: it
accepts a source beneath configured roots, performs bounded no-follow reads,
verifies evidence bindings, publishes a content-addressed managed copy, and
does not persist or return the source path.

Web projections must reject or remove absolute paths, environment material,
command lines, cookies, secrets, tokens, tracebacks, and infrastructure
details. Downloads revalidate containment, size, and content hash before
serving a managed artifact.

## Authentication, authorization, and audit

Django sessions establish identity; network placement does not. State-changing
requests require CSRF protection. Views check explicit permissions before
object lookup or mutation, and application services independently enforce the
actor and capability permission. Login failure throttling uses secret-HMAC
subjects so raw usernames and source addresses are not stored as throttle keys.

Login success, login failure, and logout use the same secret-HMAC subjects and
enter `WebAuditEvent` through the transactional outbox; credentials, raw account
identifiers, and raw source addresses are never audit fields. Failure to insert
the outbox intent is fail-closed for login and login failure. Logout always
terminates the session even if insertion fails and returns a fixed unavailable
response. A post-commit JSONL projection failure leaves the committed intent
pending and makes audit readiness fail instead of discarding the event.

Accepted actions record a correlation ID, immutable actor snapshot,
capability, opaque target identity, outcome, and relevant content hashes. Web
audit intent and ORM mutation share a database transaction. JSONL projection is
durable and idempotent through the Operations outbox, but remains a second
authority whose lag and integrity are independently observable.

Human review and final approval require explicit roles and separation of
duties. An originator or execution actor cannot independently approve their own
result; a prior reviewer cannot perform the final approval for the same
evidence. Approval also requires current-password step-up, a hash-valid PASS
result, a locked governance snapshot, resolved change requirements, and
idempotent request evidence. Automated PASS never means human approval.

Account and role lifecycle is externally governed and requires an independent
approval path. Local role mutation is unsupported: the Django admin does not
register User, Group, or Permission, and production portal code must not add,
remove, or rewrite group or direct-permission assignments. RBAC seed migrations
and isolated test fixtures may materialize reviewed roles; they are not a
runtime grant workflow.

Research exploration applies a second, object-level decision after the role
check. Dataset access uses immutable exact-ID `DATASET` grants; collection
responses omit ungranted records and detail routes return not found. Reviewed
runner, reviewer, approver, and administrator roles receive the explicit
`view_all_research_datasets` permission through a schema migration. The viewer
role requires a dataset-specific grant, so possession of generic
`research.view` alone does not disclose dataset identities.

## Capability and GUI policy

Policy meanings are contractual:

- `required`: required capabilities require a GUI workflow contract including
  validation, permission enforcement, safe projection, audit, and focused
  tests.
- `admin_only`: the capability is available only behind its explicit elevated
  workflow gates; otherwise it remains disabled.
- `cli_only`: CLI-only capabilities remain intentionally unavailable from the GUI.
  There is no generic command text box or arbitrary CLI argument surface.

Every Research CLI command has exactly one GUI policy:

| Command | Policy | GUI contract or reason |
| --- | --- | --- |
| `research-backtest` | `cli_only` | Expert standalone workflow; guarded validation may call the same engine after admission. |
| `research-walk-forward` | `cli_only` | Expert standalone workflow; required folds run only inside guarded validation. |
| `research-validate` | `required` | Durable, fail-closed validation with immutable manifest and result bindings. |
| `research-readiness` | `required` | Read-only bounded preflight projection with no path disclosure. |
| `research-freeze-dataset` | `admin_only` | Publishes an immutable input and requires elevated review. |
| `research-workload-estimate` | `required` | Deterministic resource estimate before execution. |
| `research-batch` | `cli_only` | Low-level orchestration; no generic batch browser surface. |
| `research-forward-diagnostics` | `cli_only` | Expert diagnostic overrides are intentionally unavailable. |
| `research-verify-audit` | `admin_only` | Elevated integrity verification with bounded selection. |
| `research-reproduce-run` | `admin_only` | Requires immutable source binding and a separately accepted durable workflow. |
| `research-registry-inspect` | `cli_only` | Low-level rows may expose internal evidence details. |
| `research-registry-validate` | `admin_only` | Administrative validation with a safe summary. |
| `research-mark-attempt-aborted` | `cli_only` | Break-glass lifecycle mutation is not a normal GUI action. |
| `research-export-strategy-package` | `admin_only` | Exports approved authoritative evidence. |
| `research-compare` | `required` | Compares hash-verified managed reports selected by opaque IDs. |
| `research-render-report` | `cli_only` | Web renders bounded projections rather than exposing the CLI renderer. |
| `research-governance-transition` | `admin_only` | Critical lifecycle mutation behind explicit governance policy. |
| `research-record-human-review` | `admin_only` | Records independent review against the current evidence hash. |
| `research-approve-strategy-candidate` | `admin_only` | Step-up, separation-of-duties, locked lifecycle, and idempotency gates apply. |
| `research-derivative-register` | `cli_only` | Registers a complete repository-external immutable derivative evidence bundle; no generic upload surface is exposed. |
| `research-derivative-replay` | `cli_only` | Replays a hash-bound external derivative evidence bundle without exposing paths through the GUI. |
| `research-derivative-diff` | `cli_only` | Compares immutable derivative packages as an expert evidence diagnostic. |
| `research-derivative-execute` | `cli_only` | Runs an allowlisted offline derivative simulation from repository-external immutable JSON; it exposes no broker or account authority. |
| `research-derivative-reproduce` | `cli_only` | Independently reruns a typed offline request and compares execution hashes without exposing external paths through the GUI. |
| `research.explore` | `required` | Bounded, path-free exploration of immutable research evidence with read auditing. |

GUI-only exploration, list, detail, and download capabilities are bounded
projections. They do not add research semantics.

## Job execution and concurrency

Long-running work never executes inside an HTTP request. The Django adapter
creates an immutable durable job request; the Operations job worker obtains a
PostgreSQL job lease and an experiment admission lease before invoking the
public admitted Research execution adapter.

The execution contract includes:

- an idempotency key and one-active-job database constraint;
- immutable manifest, actor, capability, request, and release snapshots;
- `FOR UPDATE SKIP LOCKED` claims where competing workers are expected;
- opaque lease tokens, monotonically increasing fencing tokens, bounded
  heartbeats, and compare-and-set publication;
- workload and combinatorial limits before dispatch;
- bounded failure projections with full diagnostics confined to trusted logs;
- an explicit result receipt bridging admission completion and the Django
  terminal job update;
- readiness failure when a receipt is unapplied or worker release identities
  differ from the configured release.

An expired lease is evidence for an authorized recovery decision, not a
license for a stale worker to publish. PostgreSQL coordination protects the
admitted publication path; filesystem and database publication still have an
explicit receipt/reconciliation boundary.

The local SQLite web profile is for development only and carries no
multi-user, multi-process, or production concurrency claim. Operational
acceptance requires the supported PostgreSQL profile and must execute all
database-specific tests without skips.

## Release and operated-runtime boundary

One release manifest binds the Git SHA, all three package versions and build
artifacts, the root lock, web and Operations migrations, native deployment
assets, and aggregate digests. Web, API, worker, backup, and diagnostics
processes receive the same release ID, SHA, and build digest. Heartbeat and
readiness checks fail closed on missing, malformed, stale, or mixed release
identity.

The official service environment sets `RESEARCH_RUNTIME_PROFILE=operated`.
Under that profile the direct `market-research` entrypoint is disabled. Only
the Operations admission service may call the explicit admitted execution
adapter after authorization, identity binding, and lease acquisition. Local
offline research retains the normal CLI when the operated profile is absent.

## Deployment boundary

The sole official deployment is
`services/research_operations/deploy/native`: systemd supervises the web,
operations API, durable workers, validator, preflight, backup, and retention
audit; host PostgreSQL and Nginx provide the supported database and TLS ingress.
Compose is a non-official portability reference and is not release evidence.

The native preflight verifies the release binding, operated profile, service
identity and permissions, owner assignments, external roots, storage
qualification, secret/key modes, certificate chain and remaining validity,
off-site policy, retention, and RPO/RTO inputs before promotion. Application
readiness separately checks migrations, database availability, workers,
validator observations, outbox lag/integrity, admission fence, receipts, and
release consistency.

## Claims and remaining acceptance gates

Source implementation, static checks, unit tests, and example preflight can
establish repository contracts. They cannot establish that a real site has
approved owners, issued and will renew certificates, connected alerts, stored
encrypted backups off site, or can meet a declared recovery objective.

Before any production claim, retain release-specific evidence for:

- fresh install and upgrade/rollback rehearsal against the selected
  PostgreSQL version;
- concurrent job, admission, audit, and fencing tests with zero skips;
- browser security and authorization tests through the actual TLS proxy;
- SIGTERM drain, supervised restart, reboot, and dependency-failure behavior;
- signed backup verification and a blank-namespace restore/activation drill;
- named service/security/data/on-call/incident/backup/recovery ownership;
- organization PKI issuance, renewal, revocation, and expiry alert routing;
- encrypted off-site receipt verification, retention/legal hold, and approved
  RPO/RTO measurements.

Until those external gates are supplied for a concrete release and host, the
documents describe an operable implementation, not an unqualified production
acceptance.
