# Internal Web GUI Architecture Contract

Status: the Research application and isolated web-adapter architecture is
accepted. Its separately authorized operational implementation exists in the
sibling `/home/vorac/work/ResearchOperations`, but the evidence recorded below
supports only a limited, single-host internal trial. It is not a production-
readiness or multi-host claim.

This document defines how an internal web GUI may adapt the offline
`market-research` library without changing its research semantics or turning the
repository into an account-connected trading system. It is a design and
repository-boundary contract. It does not authorize deployment, service
management, health-check infrastructure, operational backup/restore, or any
other operator tooling forbidden by `AGENTS.md`.

## Initial diagnosis

The repository started as an offline CLI and Python library, not a web service.
The reviewed implementation has several properties that must remain true:

- `src/market_research/research_cli/registry.py` is the complete public CLI
  command registry. Most commands currently reach research modules through CLI
  handlers, so exposing handlers directly as HTTP views would duplicate policy
  and error semantics.
- `ResearchSettings.from_env()` and `ResearchPathManager` provide the canonical
  repository-external storage roots. Immutable datasets, derived artifacts,
  reports, caches, and SQLite files must never be placed in the source tree.
- `storage_io.py` provides atomic writes for derived outputs and append-only
  writes for evidence streams. A GUI must not replace these primitives with
  ORM updates to authoritative research artifacts.
- Canonical hashes intentionally exclude runtime path fields, while some raw
  reports and CLI environment summaries contain absolute filesystem paths.
  Those payloads are evidence for trusted local researchers, not safe web view
  models.
- The research run lifecycle records `STARTED`, `SUCCEEDED`, `FAILED`, and
  `ABORTED`. It is not a durable queue and does not currently prove cancellation,
  worker leasing, restart recovery, or exactly-once execution.
- Governance actor values are research evidence, not authentication. The web
  adapter supplies sessions, RBAC, step-up password confirmation for final
  approval, and explicit originator/reviewer/approver separation. Approval now
  validates a locked governance snapshot and atomically publishes its review
  and lifecycle rows as one old-or-new JSONL replacement. The approval artifact
  is an idempotent projection of that authoritative pair; this is a filesystem
  evidence transaction, not a database/filesystem transaction.
- The root distribution has no Django or worker/runtime dependencies. This is
  deliberate and is enforced by tests.

The first safe deliverable is therefore a UI-neutral application boundary and
an isolated web adapter. It is not a thin HTTP wrapper around CLI subprocesses.

## Scope and non-scope

The architecture permits these repository changes:

- typed, framework-neutral request/result/capability contracts under
  `market_research.application`;
- a separately packaged, server-rendered internal adapter whose dependencies do
  not enter the root `pyproject.toml`;
- read-only projections for jobs, reports, validation outcomes, and immutable
  manifest metadata;
- explicit permission and GUI-policy metadata for every CLI capability;
- tests that preserve dependency direction, storage confinement, redaction,
  deterministic research behavior, and Research Semantics v2.

The following remain excluded even if a prototype adapter has framework
entrypoints:

- systemd/Kubernetes/container deployment files, reverse-proxy configuration,
  service installation, startup scripts, or production host management;
- health/readiness endpoints for operating a service, monitoring agents,
  alerting integrations, and single-instance coordination;
- operational database or artifact backup/restore, state repair, retry/backfill,
  and disaster-recovery commands;
- market-data collection, order/fill ingestion, exchange probing, private APIs,
  accounts, orders, fills, or trading controls;
- arbitrary filesystem browsing, arbitrary command execution, raw SQL, and a
  generic “run CLI” text box.

Adding any excluded capability requires an explicit, reviewed change to
`AGENTS.md` and the architecture boundaries before implementation.

## Assumptions

1. Inputs are externally prepared immutable datasets and canonical manifests.
2. The GUI is available only to an authenticated internal audience, but network
   placement alone is never treated as authentication or authorization.
3. The research library stays deterministic and framework-neutral. The same
   typed application request must mean the same thing from CLI and web adapters.
4. Canonical research artifacts, hashes, and append-only evidence remain the
   research-result authority. PostgreSQL is authoritative for web identity,
   permissions, jobs, immutable audit intents, governance coordination and
   lifecycle, and the managed imported-report catalog. Database and filesystem
   evidence cannot share one physical transaction, so validators and recovery
   receipts must bind and check both authorities.
5. Ordinary users select opaque IDs or uploaded immutable manifests; they do
   not submit host filesystem paths. The sole exception is the administrator-
   only historical report importer described below, which accepts one absolute
   selector only beneath preconfigured roots and never retains or returns it.
6. Safe execution, durable queueing, concurrency control, and recovery are
   separate completion gates. A page labelled “job” does not imply that those
   guarantees exist.

## ADR-001: framework and packaging

Decision: use a server-rendered Django adapter in a separate
`apps/internal_web` Python project, with its own dependency lock and tests. Do
not add Django, a task queue, an ASGI/WSGI server, or a database driver to the
root research distribution.

Django is preferred because the intended GUI needs mature session
authentication, CSRF protection, forms, permissions, migrations, and an audit-
friendly server-rendered surface. A separate JavaScript SPA would add a second
API/authentication surface without helping the research engine. Rich client
behavior may be added progressively only where it has a measured usability
benefit.

This ADR selects an adapter framework, not a production operating model. The
repository remains unable to claim “long-term operated service” readiness until
the security, concurrency, persistence, and ownership gates below have been
reviewed. Deployment and service operations are explicitly outside this
repository's allowed scope.

## Dependency direction

The only allowed dependency direction is:

```text
internal web views/forms
        |
        v
market_research.application   (typed requests, results, permissions)
        |
        v
existing research/domain/storage modules
```

The research package must never import Django, another web framework, or
`apps.internal_web`. The root package must remain importable and testable with
only the root dependencies. Web views must call application use cases rather
than CLI parser/dispatcher functions. CLI and web adapters may share typed
application contracts; they must not call one another.

## Storage and path contract

All persistent state remains at absolute, repository-external locations chosen
through `ResearchSettings` and `ResearchPathManager`.

- Normal browser workflows send opaque manifest/job/report IDs, never absolute
  paths. The admin-only historical importer is the explicit narrow exception:
  it accepts one allowlisted absolute source selector for bounded no-follow
  validation and immediately publishes a path-free managed copy.
- A stored web reference is a root kind plus a validated POSIX-relative path.
  Reject absolute paths, drive letters, `.`/`..`, separators embedded in a path
  segment, NUL/control characters, and symlink escapes.
- Resolve the reference under its configured root, verify containment again
  after resolution, and verify the expected content hash before rendering or
  download.
- Uploads are size-limited, parsed fail-closed, content-addressed, and written
  atomically under an approved external root. The client filename is display
  metadata only.
- Manifest and result reads are bounded before decoding and revalidate the
  recorded size/content hash. A manifest is checked for parameter/scenario
  combinatorial admission before core parsing and again immediately before
  dispatch.
- Web responses and audit details never contain configured roots, absolute
  paths, secrets, cookies, tokens, tracebacks, environment dumps, or raw command
  lines. `ArtifactReference.uri` is not safe merely because it is typed: a web
  projection must allow only an opaque/reference scheme and reject local paths.
- Raw research reports may retain absolute paths for reproducibility. Web
  projection must map them to opaque references or omit them; it must not mutate
  the authoritative report to achieve redaction.
- Derived research outputs keep atomic-write behavior. Audit streams keep
  append-only, hash-chain validation behavior.

`ResearchPathManager.from_settings()` must receive an explicit repository root
in any adapter process; relying on the process current working directory is not
a stable service boundary.

## Identity, authorization, and audit

Authentication establishes a local user identity. Repeated login failures are
throttled by secret-HMAC account and source-address subjects stored in the
metadata database; raw usernames and addresses are not retained by the
throttle. The Django adapter checks an explicit permission before object lookup
or mutation and verifies that each web job's service, request/result model,
execution mode, risk policy, and permission metadata still match the capability
catalog. CSRF protection is mandatory for every state-changing request. Shared
application services independently resolve the capability and reject a missing
actor or missing catalog permission before invoking research code. The trusted
local CLI uses an explicit wildcard actor rather than an implicit bypass.

Each accepted request gets a correlation ID and immutable actor snapshot. Web
audit events record the actor ID, capability, target opaque ID, outcome,
correlation/request ID, and content-hash bindings. Audit payloads use a strict
allow-list and never record submitted secrets or raw paths.

An administrative capability is not automatically production-ready. Human
review and candidate approval are enabled only for their explicit permissions,
use the authenticated actor rather than browser-supplied identity, prohibit the
originating owner/execution actor, and prevent a prior reviewer from approving
the same result. Final approval additionally requires the `research_approver`
role, current-password confirmation, a hash-valid PASS result, a uniquely
approval-ready registry subject, and resolution of all outstanding requirements.
The core approval service records a request ID and canonical request hash,
rechecks lifecycle, evidence, unresolved requirements, and prior-reviewer
separation while holding the governance-stream lock, and publishes the approval
review and transition together. An exact replay returns the same evidence;
changed material under the same request ID and a distinct second approval fail
closed. Raw governance transition remains unavailable. This file-lock contract
is tested on Linux, but it is not evidence for an untested shared filesystem or
supported operating database.

## Capability and GUI policy

Policy meanings:

- `required`: required capabilities require a GUI workflow contract before the
  GUI can be called complete. The workflow must include validation, permissions,
  safe projection, audit, and focused tests; a button alone is insufficient.
- `admin_only`: the capability may be exposed only after its elevated workflow
  gates are implemented and reviewed. Otherwise it stays disabled/fail-closed.
- `cli_only`: CLI-only capabilities remain intentionally unavailable from the GUI.
  This includes low-level recovery, batch, or diagnostic semantics and commands
  for which no complete guarded web contract exists. A CLI-only command may be
  backed by research operations used inside the canonical validation engine
  without its CLI handler becoming a standalone GUI action.

Every public CLI command has exactly one policy:

| Command | Policy | GUI contract or reason |
| --- | --- | --- |
| `research-backtest` | `cli_only` | Standalone execution remains an expert CLI workflow; guarded web validation invokes the same engine only after mandatory preflight. |
| `research-walk-forward` | `cli_only` | Standalone execution remains an expert CLI workflow; manifest-required folds run only inside guarded web validation. |
| `research-validate` | `required` | Fail-closed validation with hash-bound evidence and structured diagnostics. |
| `research-readiness` | `required` | Read-only preflight projection; no path disclosure. |
| `research-freeze-dataset` | `admin_only` | Publishes an immutable input and therefore needs elevated review. |
| `research-workload-estimate` | `required` | Deterministic resource estimate shown before execution. |
| `research-batch` | `cli_only` | Low-level subprocess orchestration; no generic batch web surface. |
| `research-forward-diagnostics` | `cli_only` | Advanced diagnostic overrides remain an expert CLI workflow. |
| `research-verify-audit` | `admin_only` | Elevated integrity verification with bounded input selection. |
| `research-reproduce-run` | `admin_only` | Long-running evidence reproduction with immutable source bindings. |
| `research-registry-inspect` | `cli_only` | Low-level registry-row inspection can expose internal evidence details. |
| `research-registry-validate` | `admin_only` | Administrative validation with safe summarized results. |
| `research-mark-attempt-aborted` | `cli_only` | Break-glass lifecycle repair remains explicitly CLI-only. |
| `research-export-strategy-package` | `admin_only` | Exports approved authoritative evidence and needs elevated authorization. |
| `research-compare` | `required` | Compares two to ten visible, hash-verified decision reports selected only by opaque report IDs; server paths are never accepted or returned. |
| `research-render-report` | `cli_only` | The adapter renders bounded summaries; it does not implement the CLI report-rendering contract. |
| `research-governance-transition` | `admin_only` | Critical authoritative state transition; disabled until governance gates pass. |
| `research-record-human-review` | `admin_only` | Records change requests or rejection against the current result hash with application-layer authorization and originator separation. |
| `research-approve-strategy-candidate` | `admin_only` | Records a hash-bound, request-idempotent final approval with an approver-only role, password step-up, locked lifecycle/evidence checks, atomic governance-pair publication, and reviewer/originator separation. |

GUI-only query capabilities such as `jobs.list`, `jobs.detail`, `reports.list`,
`reports.detail`, and `reports.download` are projections over bounded metadata or
verified artifacts. They do not add new research semantics.

## Execution and concurrency contract

Long-running required workflows must not execute inside an HTTP request. Before
they can be enabled, a reviewed coordinator must provide all of the following:

- durable job identity and idempotency key;
- a database constraint allowing at most one active job per owner, including
  concurrent submissions with different request hashes;
- atomic claim/lease semantics with a single active executor per job;
- bounded concurrency based on existing resource-limit policy;
- immutable request snapshot and actor/capability snapshot;
- explicit terminal outcomes mapped without inventing domain lifecycle events;
- explicit lease-expiry semantics that never overwrite canonical artifacts;
- progress derived from durable evidence rather than process-local callbacks;
- safe failure summaries, with full tracebacks confined to trusted local logs.
- bounded parameter/scenario/work-unit admission at upload and dispatch.

The prototype adapter permits cooperative cancellation only at explicit
application-service boundaries. That behavior does not prove that an arbitrary
research execution can be interrupted safely, and it must not be treated as
restart reconciliation or state repair. Retry and automatic recovery controls
remain unavailable. Shared-filesystem and supported-database behavior still
require the external concurrency gates below.

An expired lease is observational evidence only. A repository worker must not
automatically requeue, fail, cancel, clear a lease, increment an attempt, or
otherwise mutate a `RUNNING` or `CANCEL_REQUESTED` job merely because
`lease_expires_at` is in the past. `run_worker_once` claims only `QUEUED` jobs,
so an expired non-queued job remains unchanged and visible for investigation.
Any reconciliation or repair decision requires a separately reviewed and
authorized operational layer. Such a layer cannot be implemented in this
repository while `AGENTS.md` forbids state repair and operator tooling.

ORM state transitions and immutable audit intents commit in the same database
transaction. A robust `on_commit` callback invokes a single-event projection
primitive; projection failure leaves a detectable pending intent and cannot
reclassify the committed job. The event-ID append is idempotent under the JSONL
stream lock, so an append-before-marker interruption can be adopted without a
second row. Validation distinguishes pending, duplicate, orphan, unmarked,
missing, malformed, and hash/payload-mismatched evidence. This remains an
eventually consistent outbox boundary, not an atomic database/filesystem
transaction. This repository intentionally provides no scanner, lease,
backoff, dead-letter queue, retry loop, or repair command.

The metadata database globally binds each web-uploaded `experiment_id`, and its
migration fails closed on legacy duplicates. In addition, both CLI and web
`research-validate` application paths bind `experiment_id` to the canonical
manifest hash in one repository-external append-only authority before invoking
the engine. Split artifact/report mount layouts require an explicit common
authority path and otherwise fail closed. Identical bindings are idempotent; a
different manifest loses the same-ID race before validation output. This is
manifest consistency, not actor ownership or exclusive execution. Standalone
backtest/walk-forward and unregistered legacy namespaces are not covered, and
the experiment path still is not a run-ID namespace.

SQLite remains the local-development metadata profile and carries no multi-user
or multi-worker support claim. The isolated web package has a strict PostgreSQL
connection-settings boundary and an optional psycopg extra. On 2026-07-16 the
complete web suite ran against PostgreSQL 16.14 over TLS `verify-full` with
`158 passed, 0 skipped`; all eight PostgreSQL-specific concurrency and
governance-atomicity tests executed. This is real single-host database evidence,
not evidence for multi-host storage, failover, every prior-release upgrade, or a
site production database policy.

## Phased completion criteria

### Phase A — boundary foundation

Complete when every CLI command is classified, typed UI-neutral contracts exist,
root/web dependency isolation is enforced, external path rules have focused
tests, and CLI behavior remains compatible. This phase does not make a web
service operational.

### Phase B — read-only GUI

Complete when an authenticated user can list and view bounded job/report
projections, download only hash-verified approved artifacts without
absolute-path leakage, and authorization/audit negative tests pass. The UI must
show evidence scope, schema version, hashes, dataset binding, parameters,
execution assumptions, and seed where applicable. The comparison catalog
indexes visible succeeded web-validation jobs and explicitly imported decision
reports, and re-verifies managed content-addressed copies on every read. The
import workflow is admin-only: an administrator may submit an absolute source
path only when it is below a server-configured allowlisted root; bounded
no-follow reads, explicit expected bindings, transactional catalog/audit intent,
and owner or organization visibility apply. The original source path is never
retained or served. Arbitrary discovery, CLI report rendering, and reproduction
remain outside Phase B.

### Phase C — research execution

Complete when readiness and workload estimates are mandatory preflight steps;
the guarded validation workflow uses a typed application service and preserves
its manifest-required backtest/walk-forward engine semantics; durable job
claim/idempotency and fail-closed lease-expiry behavior are proven; resource
limits are enforced; and deterministic equivalence with the CLI is covered by
integration tests. Standalone backtest/walk-forward GUI actions remain
CLI-only. Restart reconciliation is an external operational gate, not an
implementation requirement this repository may satisfy.

### Phase D — elevated workflows

Complete only after each `admin_only` capability has an explicit permission,
step-up confirmation, concurrency-safe transaction/evidence design,
separation-of-duties policy where applicable, append-only audit coverage, and
positive and negative authorization tests. Capabilities without all gates stay
disabled. Human review and approval now satisfy the web authorization, step-up,
hash-binding, locked concurrency, idempotent replay, and separation gates, but
Phase D as a whole remains incomplete: raw transitions, reproduction, and
exports are not implemented as web workflows.

### Phase E — operational adoption

Not implementable *inside this repository* under the current `AGENTS.md`. The
separate `ResearchOperations` trust domain now implements PostgreSQL
coordination, persistent workers, guarded probes, TLS/proxy configuration,
backup fencing, signed verification, and isolated recovery activation. Its
single-host acceptance evidence is summarized below and specified in the
handoff document. Site identity integration, immutable-image execution,
production certificate lifecycle, off-site retention and approved RPO/RTO,
incident ownership, and any multi-host promotion remain external gates. Until
those gates are accepted, Phase E permits only a limited internal trial and not
a general long-term-production claim.

## Verification gates

Repository CI must keep focused tests for dependency direction, root dependency
isolation, capability-policy completeness, relative-path confinement, upload
validation, hash verification, redaction, CSRF, permissions, audit-chain
validation, unchanged strategy/research semantics, and non-mutation of expired
jobs. `ResearchOperations` additionally owns migration, browser workflow,
security-header, live concurrency, restart/reconciliation, backup/recovery, and
supported-database tests in its own validation pipeline.

No phase is complete because files merely exist. Completion requires its tests
to pass and its residual risks to be accepted by the responsible reviewer.

## Single-host implementation and acceptance evidence

The 2026-07-16 acceptance run used PostgreSQL 16.14 and repository-external
Linux ext4 roots. It established the following without moving operational code
into this repository:

- the Research suite completed with `596 passed`; the live-PostgreSQL web suite
  completed with `158 passed, 0 skipped`, and the required browser E2E completed;
- the Operations suite completed with `43 passed, 0 skipped`, including 16 live
  PostgreSQL integration tests; lint, format, shell syntax, lock, compile, and
  sdist/wheel build checks passed;
- two audit-delivery workers and one research-job worker remained ready, a
  stopped delivery worker made workflow readiness fail closed, and restart
  restored it; a restored outbox event was projected exactly once;
- native nginx and Gunicorn passed HTTP-to-HTTPS redirect, valid CA/hostname,
  invalid CA and hostname, security-header, secure-cookie/CSRF login, report
  access, employee-ingress isolation, operations mTLS, Basic authorization,
  diagnostics, metrics, and both readiness checks;
- an installed-package smoke test first exposed that web templates/static files
  were missing from the wheel and that an installed adapter could infer the
  wrong source root. Package data and an explicit absolute
  `RESEARCH_OPS_SOURCE_ROOT` fixed both defects. The final bundle digest was
  `sha256:547577d7239b5276cb35d42cbcf2c3bfcb57cb6ef72c073c81f2adc6d6b64674`;
- backup `df9ac410-a085-452e-be09-50c27a312bee` produced signed manifest
  `sha256:ec7ea9a51985e90696eb415478104afe2aaaac715007414c7c0a18e21ebf0fe4`.
  A blank isolated restore passed all 17 checks, its signed receipt hash was
  `sha256:b3cbfad45032debf4cab9d3a7768c309e0aa3d0259a3b9094783560d7efc814d`,
  and deterministic drill ID `ec28ec13-f19d-5bc1-aa79-f1156d708ff1` was recorded;
- checked database ACLs were reapplied before restored service startup.
  Recovery activation opened generation 4 and an exact retry returned the same
  activation and drill evidence. The restored service reached readiness with
  two delivery workers and one research worker, completed CSRF login and report
  access, projected event `4a7c1f83-0b23-49dc-8071-51330a7ea76a` exactly once,
  failed closed with `database_unavailable` against a dead endpoint, and became
  ready again after the valid database endpoint was restored.

The UI now describes approval as an active guarded workflow rather than a
disabled feature, hides review/approval forms after final approval while
explaining exact replay, and displays the historical import form only to an
authorized administrator. These are application-state and usability guarantees,
not substitutes for the operational gates below.

## Final transaction and operational responsibility map

| Boundary | Current repository guarantee | Deliberately external or unresolved |
| --- | --- | --- |
| Application transaction | Capability authorization is independently checked; job mutations use database transactions, conditional updates, and constraints; the live PostgreSQL matrix passed. | Multi-host, failover, and site identity-policy acceptance. |
| Audit outbox transaction | Related ORM state and immutable `WebAuditEvent` intent commit together. | The JSONL append cannot share that database transaction. |
| JSONL projection | Segmented stream, event-ID idempotency, lock and fencing checks, persistent discovery/lease/backoff/DLQ worker, incremental validation, and independent full validation. | The database/filesystem gap remains detectable rather than physically atomic; site alert delivery and retention ownership remain external. |
| Governance approval | PostgreSQL row locking, unique decision/duty constraints, DB-authoritative lifecycle, locked old-or-new core evidence publication, and exact request replay passed live concurrency tests. | Database and filesystem cannot physically commit together; recovery must continue to bind both. Multi-host filesystem qualification remains absent. |
| Database locking | PostgreSQL 16.14 `verify-full` tests passed with no backend skips; SQLite remains development-only. | Prior-release upgrade, failover, capacity, and patch policy belong to the site. |
| CLI/web ID consistency | Web DB uniqueness, common manifest identity, and Operations admission/fencing serialize admitted web and CLI execution. | Standalone Research CLI entrypoints can bypass Operations admission and must be excluded by deployment policy; eventual run-ID namespace migration remains open. |
| Artifact catalog | Visible web results and explicitly imported, owner/organization-scoped managed copies use opaque IDs and are hash-revalidated on every read. | Arbitrary discovery is forbidden; reproduction remains disabled. |
| Reproduction | Trusted local CLI contract only. | Web reproduction remains disabled until catalog, revision/environment binding, isolated outputs, admission, authorization, and supervision gates pass. |
| Liveness/readiness/diagnostics | Operations WSGI supplies fixed liveness, split readiness, authenticated bounded diagnostics and label-free metrics; negative transitions were exercised. | Persistent monitoring transport, alert ownership, and SLO approval remain site responsibilities. |
| Backup and recovery | Operations supplies a two-phase fence, signed manifest/receipt, exact resume, blank-namespace verifier, deterministic drill record and explicit idempotent activation; the exact final bundle was restored. | Off-site copy, encryption/retention/key policy, scheduled drills and approved RPO/RTO remain site responsibilities; no repair mode is allowed. |
| Deployment/TLS/workers | Native nginx/Gunicorn TLS and persistent worker behavior passed single-host acceptance. | The compose reference was not executed as an immutable production image; site PKI lifecycle, supervisor integration, multi-host storage and incident ownership remain open. |

The concrete external acceptance criteria, recovery order, probe semantics,
environment and mount contract, PostgreSQL concurrency matrix, and historical
report/reproduction threat model are defined in
`docs/internal-web-operations-handoff.md`. The sibling
`ResearchOperations` project is the authorized operational trust domain. The
separate `Operation` repository is a trading runtime with a different active
scope and is not an authorized home for these responsibilities.
