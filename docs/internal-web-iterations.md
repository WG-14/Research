# Internal Web GUI Iteration Log

This is an evidence log, not a completion claim. It records only work actually
performed in this repository. The requested production operating model remains
constrained by `AGENTS.md`.

## Iteration 1/10 — repository diagnosis

**Observed**

- The project is an offline research CLI/library with no supported runtime
  service contract.
- Research storage is configured through absolute repository-external roots;
  canonical writes and append-only evidence already exist.
- Most CLI commands do not share one typed UI-neutral application service.
- Raw reports and CLI environment summaries can contain absolute paths.
- Existing run lifecycle, governance evidence, and registries do not establish
  durable queue, multi-user transaction, cancellation, or recovery guarantees.
- Repository rules explicitly exclude deployment, service management,
  health-check infrastructure, operational backup/restore, account access, and
  operator tooling.

**Change**

No source was changed during diagnosis. The CLI registry, settings/path manager,
storage and hashing code, run lifecycle, governance writes, registry behavior,
root dependencies, and CI tests were inspected before the architecture was set.

**Evidence**

The team baseline was run with capture disabled because this environment could
not create pytest's capture temporary file:

```text
.venv/bin/pytest -q -s tests/test_research_application_service.py \
  tests/test_research_cli_boundary.py tests/test_run_lifecycle.py
9 passed
```

**Remaining risk / next decision**

The user requested a long-term operated internal system, but this repository is
not allowed to contain its operating infrastructure. Separate framework and
operational-readiness decisions were required.

## Iteration 2/10 — architecture and capability policy

**Discovered**

A direct web wrapper around CLI subprocesses would expose path-oriented inputs,
duplicate validation, and make low-level recovery commands accidentally
available. Required, elevated, and deliberately CLI-only workflows needed an
explicit catalog.

**Change**

- Added `docs/internal-web-architecture.md` with a Django adapter ADR,
  dependency direction, storage/redaction rules, authorization/audit rules,
  execution gates, exclusions, risks, and phased completion criteria.
- Classified every public CLI command as `required`, `admin_only`, or
  `cli_only`; documented GUI-only read projections separately.
- Kept deployment, health, backup/restore, service management, and operational
  recovery outside the design's implementable repository scope.

**Verification**

The classification is machine-checked against the CLI registry and application
capability catalog in the next iteration. No operational readiness is claimed.

**Remaining risk / next decision**

Typed catalog entries do not implement handlers. In particular, queued
execution and elevated governance workflows remain fail-closed until their
phase-specific gates are met.

## Iteration 3/10 — repository enforcement and narrow CLI repair

**Discovered**

- `research-batch --continue-on-error` parsed into an attribute that the
  dispatcher ignored, while dispatch used `fail_fast`.
- Each batch child command included `--notification-policy disabled`, an option
  not registered by `research-backtest`.
- Architectural path and dependency rules were documented but not all were
  regression-tested at the intended web boundary.

**Change**

- Made `--fail-fast` and `--continue-on-error` mutually exclusive values of the
  same parser destination, retaining continue-on-error as the default.
- Removed the unregistered child option from batch backtest invocation.
- Added focused tests for CLI-policy completeness, root/web dependency
  isolation, repository-external environment paths, safe path segments,
  user-facing run-summary redaction, and CLI-only environment-summary isolation.
- Added focused parser and child-command regression tests for the two batch
  defects.

**Verification**

Focused validation is recorded only after it has actually completed in a later
iteration.

**Remaining risk / next decision**

The redaction test covers the existing `ResearchRunSummary` projection, not all
future view models. Any artifact-reference model still needs web-specific
allow-list and containment/hash checks. Full queue, governance concurrency,
authentication, browser, and supported-database behavior remain future work.

## Iteration 4/10 — focused validation

**Discovered**

The capability catalog added alongside this work contained one entry for every
CLI command and matched the documented policy exactly. The new dependency and
path guards did not require changes to supported research semantics.

**Change**

No additional production code was changed in this iteration. The documentation
and focused tests were checked for whitespace errors, then run together with the
existing application-service, CLI-boundary, and run-lifecycle regressions.

**Verification**

```text
.venv/bin/pytest -q -s tests/test_research_application_service.py \
  tests/test_run_lifecycle.py tests/test_internal_web_architecture_contract.py \
  tests/test_research_batch_cli.py tests/test_research_cli_boundary.py
23 passed

git diff --check
passed (no output)
```

**Remaining risk / next decision**

This validates the application/catalog boundary and the two CLI fixes, not an
operated web system. Browser flows, supported-database concurrency, durable job
coordination, full authorization matrices, operational ownership, and all
Phase C–E gates remain unproven or excluded.

## Iteration 5/10 — shared application boundary

**Discovered**

The CLI registry classified all public commands, but only the existing research
modules defined their execution semantics. A web adapter needed immutable typed
requests/results and direct Python services without importing CLI dispatch or
re-implementing validation behavior.

**Change**

- Added strict, frozen Pydantic request/result contracts, structured public
  errors, adapter construction, and a UI-neutral capability catalog under
  `src/market_research/application`.
- Implemented shared readiness, workload-estimate, combined preflight, and
  validation services over the existing research engine and path manager.
- Routed CLI validation through the shared service. The application layer does
  not import the CLI or web package and does not launch subprocesses.
- Kept one catalog entry for every public CLI command, with GUI-only bounded
  query entries represented separately. Each entry names its `service_id`, so
  a shared application method, bounded query service, and legacy CLI handler
  are distinguishable rather than inferred from the command name.

**Verification**

```text
.venv/bin/pytest -q -s tests/test_application_contracts_and_capabilities.py
6 passed
```

**Remaining risk / next decision**

Only readiness, workload/preflight, and validation have concrete shared
services. Several catalog entries still use generic request/result contracts,
and catalog permission strings are metadata rather than complete authorization
enforcement. Backtest, walk-forward, comparison, rendering, reproduction, and
elevated workflows remain incomplete or unavailable in the GUI.

## Iteration 6/10 — isolated web backend and persistent job metadata

**Discovered**

An ordinary HTTP request could not safely own long-running research execution.
The adapter also needed repository-external upload storage, opaque references,
object-level authorization, immutable request evidence, and durable job
metadata without making ORM rows authoritative research artifacts.

**Change**

- Added the separately packaged `apps/internal_web` Django adapter with its own
  dependencies, settings, migrations, and tests; Django remains outside the
  root research distribution.
- Added fail-closed settings checks, RBAC seed data, bounded manifest upload and
  content-addressed storage, opaque contained artifact references, actor and
  correlation snapshots, safe audit details, and hash-verified result loading.
- Added idempotent enqueue, atomic queued-job claim with lease tokens,
  cooperative progress/cancellation boundaries, terminal result contracts, and
  a direct Python dispatcher over the shared application service. HTTP views do
  not invoke CLI subprocesses or execute the engine.
- Bound validation enqueue and execution to a successful, hash-verified
  preflight result for the same immutable manifest.

**Verification**

Focused backend selectors cover settings/audit, forms/models, storage/security,
and queue/worker state transitions. Their final result is recorded in Iteration
8 with the integrated adapter run.

**Remaining risk / next decision**

The metadata database used by the tests is not a reviewed production operating
database. Database transitions and the external append-only audit write are not
one atomic commit. The web capability enum still duplicates part of the shared
catalog instead of deriving all authorization from it, and a common experiment
identifier can still create cross-user derived-output collision risk. None of
these files establishes deployment, service ownership, supported-database
concurrency, or backup/restore readiness.

## Iteration 7/10 — server-rendered workflow and browser evidence

**Discovered**

The safe ordinary-user path needed to explain evidence state rather than expose
research internals: login, immutable manifest selection, mandatory preflight,
validation, progress, result interpretation, and a verifiable download. Reviewer
visibility also needed explicit object scoping.

**Change**

- Added server-rendered login, dashboard, manifest upload/detail, job
  detail/status, safe result download, and reviewer queue routes with a Korean
  research workflow and responsive styling.
- Applied session authentication, CSRF enforcement, permission and owner
  scoping, opaque IDs, private/no-store responses, structured error IDs, and
  allow-listed presentation models that do not disclose configured roots.
- Added integration tests using the real readiness and validation engines, plus
  a Playwright browser path from login through upload, preflight, validation,
  and hash-verifiable download.

**Verification**

The Django integration selectors exercise login, object scoping, CSRF, the real
engine, status projection, and download hashing. The browser test is allowed to
skip locally when Chromium system prerequisites are unavailable; CI installs
Chromium explicitly. The final local counts are recorded in Iteration 8.

**Remaining risk / next decision**

The reviewer queue is a read projection, not a complete human-review or
approval transaction. Backtest, walk-forward, report comparison/rendering,
reproduction, and elevated workflows are not complete GUI workflows. Local
browser execution cannot prove the target network, identity, database, or
host-operating model.

## Iteration 8/10 — evidence parity, CI isolation, and recovery boundary

**Discovered**

- Canonical evidence comparisons still retained nested runtime/path observations
  in some payloads, so identical CLI and web research could hash differently.
- Root pytest discovery also needed to stay isolated from the separately
  packaged Django tests.
- The original `required` labels overstated standalone backtest, walk-forward,
  comparison, and CLI report-rendering workflows that the adapter does not
  implement.
- The prototype worker automatically requeued expired leased jobs. That is state
  repair, which is expressly forbidden by `AGENTS.md` and contradicted the
  architecture contract.

**Change**

- Made logical evidence hashing recursively project runtime/path observations
  while retaining source artifact hash verification, and added real CLI/web
  validation parity coverage.
- Restricted root pytest discovery to `tests` and added a separate internal-web
  CI job that syncs its own lock, installs Chromium, checks Django configuration
  and migration drift, and runs the adapter suite. This is CI configuration,
  not evidence that a remote CI run or production deployment occurred.
- Classified standalone backtest, walk-forward, comparison, and CLI report
  rendering as `cli_only`. Guarded validation still invokes its canonical
  manifest-required backtest/walk-forward engine stages after mandatory
  preflight; that does not expose those commands as standalone GUI actions.
- Removed automatic orphan recovery from the worker and job service. A worker
  now claims only `QUEUED` jobs; an expired `RUNNING` or `CANCEL_REQUESTED` job
  remains unchanged. A regression test asserts the status, lease token, and
  terminal fields are not repaired or rewritten.

**Verification**

```text
.venv/bin/pytest -q -s tests/test_application_validation_real_parity.py \
  tests/test_research_logical_evidence_hashing.py \
  tests/test_application_contracts_and_capabilities.py
9 passed

cd apps/internal_web
.venv/bin/pytest -q -s --create-db tests/test_jobs_worker.py
10 passed

.venv/bin/pytest -q -s --create-db tests/test_configuration_audit.py \
  tests/test_forms_models.py tests/test_security_storage.py \
  tests/test_jobs_worker.py
27 passed

.venv/bin/pytest -q -s --create-db tests/test_views_execution.py \
  tests/test_browser_e2e.py
7 passed, 1 skipped

.venv/bin/pytest -q -s --create-db
34 passed, 1 skipped
```

The one local skip reports missing Playwright Chromium system prerequisites;
the separate CI job installs them before running the same suite. Django's
`makemigrations --check --dry-run` reported no changes, root collection found
545 tests under the configured root `tests` directory, and `git diff --check`
passed with no output. These are local validation results, not a claim that the
new remote CI job has run.

## Iteration 9/10 — evidence, resource, and concurrency hardening

**Discovered**

- A redacted download cannot retain the authoritative report hash after its
  bytes change; it needs its own projection hash and an explicit source binding.
- Manifest display/dispatch and result verification needed one bounded,
  hash-verified read policy. Raw parameter and execution-model arrays could
  create a very large Cartesian product before the worker could apply a
  cancellation check.
- The view's per-user active-job check was vulnerable to two concurrent,
  different submissions. A terminal success followed by an audit write failure
  could also enter the worker catch-all and attempt an invalid FAILED rewrite.
- Several user-facing strings implied a persistent worker and approval workflow
  that this repository does not provide.

**Change**

- Published downloads as independently hashed redacted projections containing
  `source_result_hash`; authoritative reports remain unchanged and are verified
  before projection.
- Added the streaming 2 MiB upload handler, bounded no-follow manifest/result
  reads, recorded-size/hash checks, and fixed public application-error
  projections that discard raw messages, details, and embedded server paths.
- Counted workload candidates without materializing their Cartesian product.
  The web adapter now rejects raw manifests above conservative candidate,
  scenario, or work-unit limits before core parsing and rechecks them at
  dispatch.
- Added a conditional database uniqueness constraint for one active job per
  owner and explicit race handling. Validation remains bound to a PASS preflight
  at enqueue and dispatch, and terminal audit failure can no longer reclassify a
  successful job.
- Added actor-permission snapshots, PASS-only review projections, a PWA
  manifest/icon, strict boolean settings, an explicit loopback HTTP cookie
  override, and UI wording that accurately labels approval and worker gaps.

**Verification**

```text
root focused application/hash/batch/workload/lifecycle selectors: 36 passed
web configuration/forms/storage/jobs/views selectors: 50 passed
terminal audit + bounded result + real validation selectors: 3 passed
Django system check: no issues
migration drift: no changes detected
```

The required browser run initially could not start because this WSL image lacked
`libnspr4`, `libnss3`, and `libasound`. Those packages were downloaded and
extracted into `/tmp` without modifying the host. One first actual-flow attempt
then returned a failed validation job; the focused real-validation selector
passed, and two subsequent complete login-to-download browser runs passed in
13.75 s and 13.43 s. This is useful browser evidence, but it is not supported-
database or production-host evidence.

**Remaining risk / next decision**

SQLite concurrency, cross-user `experiment_id` output collisions, audit/ORM
atomicity, persistent worker ownership, restart handling, retry, governance
transactions, and application-service permission enforcement remain open.

## Iteration 10/10 — shutdown recovery and final package boundary

**Discovered**

The workstation stopped during the one repository-wide pytest invocation. Its
final summary was never written. The pytest failure cache contained obsolete
node IDs, while the retained temporary directories identified three tests that
were active near shutdown. WSL had inherited `TEMP` and `TMP` under `/mnt/c`,
where Python forkserver Unix sockets fail with `Errno 95` and pytest capture also
showed unreliable temporary-file behavior.

**Change**

- Preserved every existing tracked and untracked change; no reset, revert, or
  cleanup of user work was performed.
- Documented Linux `/tmp` for WSL verification and hardened CI distribution
  assertions so the common application package must be present while
  `apps/internal_web` must remain absent from the root distribution.
- Completed this recovery/evidence record. This is an iteration-budget record,
  not an operational-readiness declaration.

**Verification**

```text
three shutdown-adjacent focused selectors on Linux /tmp: 3 passed
root and web uv lock --check: passed
root and web compileall: passed
uv build: wheel and sdist built successfully
archive inspection: application package included; web/Django/tests excluded
wheel file count: 171; sdist file count: 193
git diff --check: passed
```

The interrupted full root suite was not invoked a second time. Repository policy
allows one full invocation and then focused reruns, so recovery used the three
retained selectors plus all directly changed boundaries. A clean, uninterrupted
full suite in CI remains required before merge.

**Remaining risk / next decision**

Phase E is still not implementable under `AGENTS.md`: deployment and service
management, a supported multi-user database/worker supervisor, health checks,
operational backup/restore, state repair, and operator tooling are forbidden in
this repository. Lease expiry remains an observable unresolved condition, not
an automatic retry signal. The adapter must not be used or described as a
long-term operated internal service until those responsibilities exist in an
authorized operational project and its integration gates pass.

## Post-recovery hardening — remaining P1/P2 web boundaries

This section continues the recovered work without creating an eleventh numbered
iteration or changing the recorded iteration budget.

**Discovered**

- Application requests carried an actor snapshot, but the common service did
  not independently reject a missing capability permission.
- Experiment-scoped core outputs made duplicate web `experiment_id` values a
  cross-user collision risk. ORM transitions and the JSONL audit projection
  also lacked a durable common commit point.
- The completed validation jobs provided a safe report visibility index, but
  the GUI had no bounded catalog/comparison workflow. Arbitrary historical CLI
  reports cannot be safely discovered without an authoritative index.
- The core governance contract supported human review and candidate approval,
  but the web adapter lacked authenticated actor binding, role separation,
  password step-up, and negative workflow tests.
- Login attempts had no persistent application throttle.

**Change**

- Added catalog-driven authorization to preflight, readiness, workload,
  validation, comparison, human review, and candidate approval application
  services. The CLI now supplies an explicit trusted wildcard actor.
- Globally constrained web manifest `experiment_id`, with a fail-closed
  duplicate preflight migration and race handling. Added database-backed,
  secret-HMAC username/source-address login throttling with bounded policy
  settings and generic failures.
- Added an immutable audit outbox. ORM state and its audit intent commit in one
  database transaction; post-commit JSONL projection is hash-chained and
  cross-checked. Projection failure leaves a pending intent and never rewrites
  the completed job. No retry or repair mechanism was added.
- Added a visible-validation report catalog and two-to-ten report comparison.
  Opaque IDs are derived from report hashes; every read revalidates bounded
  canonical summary/report paths, schemas, hashes, ownership, experiment,
  manifest, run, outcome, selection, and comparison bindings. Raw paths are
  neither accepted nor projected.
- Added web review and final candidate approval over the shared governance
  service. The workflow records only change requests/rejection through the
  review form, requires an explicit approver role and current password for final
  approval, binds decisions to the current result hash, rejects originators and
  prior reviewers, validates registry/lifecycle evidence, and relies on the
  canonical lifecycle gate to reject a sequential duplicate approval.
- Corrected report/review routes, templates, capability workflow contracts, safe
  manifest conflict presentation, and transaction-test restoration of RBAC data
  migrations.

**Verification**

```text
application/governance/comparison/reporting focused selectors: 37 passed
architecture and capability policy contract: 13 passed
CLI/batch/hash/parameter/lifecycle regressions: 16 passed
web backend focused invocation: 78 passed, 1 setup error
transaction-test isolation repair and failure-order rerun: 5 passed
governance-related web selectors: 25 passed
report catalog/comparison web selectors: 7 passed
required Playwright login-to-download flow: 1 passed
Django system check: no issues
migration drift: no changes detected
root and web uv lock checks: passed
root and web compileall: passed
root wheel and sdist build: passed
git diff --check: passed
```

The one web setup error was caused by transactional tests flushing RBAC groups
created by a data migration. Marking those tests for serialized rollback made
them order-independent; the exact audit-outbox-to-worker failure order then
passed. Counts for the governance and report selectors overlap the 78-test web
invocation and are listed as focused evidence, not an additive suite total. The
interrupted root-wide pytest invocation from Iteration 10 was not repeated.

**Remaining risk / next decision**

The outbox is not an atomic database/filesystem transaction, and automatic
projection retry is deliberately absent. Concurrent multi-row governance
approval is not proven atomic, SQLite is not a supported multi-user or
multi-worker operating database, identifiers used only by external CLI runs are
not reserved by the web metadata constraint, and reproduction remains disabled
because its current path-oriented contract cannot be exposed safely. Persistent
workers, deployment, TLS termination, health checks, restart reconciliation,
and operational backup/restore remain forbidden here and must be delivered and
validated in a separately authorized operational repository.

## Current operational-hardening cycle — iteration 1/6: recovery and policy boundary

**Diagnosis**

- The requested reference HEAD was `b1bb6877`, but the recovered repository was
  already at `cbe8b125` on `main`, tracking the same `origin/main` commit.
- The single worktree was clean: no tracked, untracked, or staged changes; no
  merge, rebase, cherry-pick, or revert operation; no conflict marker, reject,
  or editor-recovery file was found. The previously described implementation
  had therefore been committed outside this recovery cycle rather than left as
  an interrupted worktree patch.
- Repository policy still forbids a persistent worker, retry/backfill or repair
  workflow, service management, health-check infrastructure, deployment, and
  operational backup/restore. The adjacent `Operation` repository exists, but
  its current lot-native batch contract does not authorize ownership of this
  unrelated service.

**Design**

The cycle was split into application-owned safety primitives and an external
operations acceptance contract. Existing changes would be preserved without
reset, revert, restore, checkout, stash, staging, branch changes, or deletion.
Unsupported operational components would not be disguised as application
features.

**Implementation**

No source mutation was appropriate in this diagnosis iteration. The repository,
worktree, state files, instructions, prior iteration evidence, and adjacent
operations boundary were inspected before implementation resumed.

**Verification**

`git status`, tracked and staged diffs, five recent commits, `git worktree list`,
Git operation markers, conflict markers, and interrupted-file patterns were
checked. The actual clean `cbe8b125` baseline was used for all following work.

**Rediagnosis**

The highest application-owned risks were the non-idempotent audit projection
boundary and the non-atomic multi-row governance approval. Infrastructure-owned
retry scheduling, health endpoints, backup jobs, and supervisors remained out
of scope by policy.

## Current operational-hardening cycle — iteration 2/6: outbox projection integrity

**Diagnosis**

An ORM transaction could persist an audit intent while post-commit JSONL
projection failed. Re-executing projection did not have a public, bounded
primitive with a stable result, and the validator did not prove exact identity
between every marked ORM intent and its hash-chain row.

**Design**

- Keep the database/filesystem boundary explicitly eventually consistent.
- Make one event ID the idempotency key, serialize appends under the existing
  POSIX lock, and reject the same ID with different payload material.
- Mark projection only after append succeeds; on replay, verify the existing
  row rather than silently treating the marker as proof.
- Expose one event-at-a-time processing for an external authorized worker. Do
  not add scanning, leases, retry timing, dead-letter mutation, or an operator
  command prohibited by repository policy.

**Implementation**

- `apps/internal_web/src/portal/audit.py` now records schema-v2 delivery mode,
  schedules robust post-commit projection, exposes
  `project_web_audit_event`, and returns `PROJECTED` or `ALREADY_MARKED` only
  after exact chain/payload verification.
- `src/market_research/research/hash_chain.py` makes an event-ID append
  idempotent under a lock and reports a conflicting replay fail-closed.
- Audit validation now detects malformed rows, duplicate IDs, missing or
  orphaned projections, marker mismatches, payload mismatches, and hash-chain
  corruption with structured failures.

The transaction boundary remains: ORM state plus outbox intent are atomic;
JSONL append and the projected marker are subsequent idempotent steps. A crash
can leave an unmarked intent, but cannot make an unverified marked intent look
healthy to the validator.

**Verification**

Focused hash-chain, audit-outbox, worker, and configuration selectors passed in
their component runs. The public primitive and marked-event revalidation tests
passed (`12 passed`), including simulated projection loss and replay. The
integrated web result is recorded in iteration 6.

**Rediagnosis**

Idempotent single-event processing and diagnosis are implemented. Automated
claim/lease/backoff/dead-letter scheduling remains intentionally absent and
must be supplied by an authorized operations system using this primitive.

## Current operational-hardening cycle — iteration 3/6: approval concurrency and atomic evidence

**Diagnosis**

Candidate approval validated lifecycle and review evidence before multiple
append operations. Concurrent requests could therefore observe the same state,
and a failure between the approval review and lifecycle transition could expose
half of the logical decision. A response projection could also overwrite an
unrelated existing file.

**Design**

- Revalidate all separation-of-duties, outstanding-review, artifact, and
  lifecycle conditions while holding the canonical evidence lock.
- Publish the approval review and `RESEARCH_APPROVED` transition as one atomic
  hash-chain replacement.
- Bind an explicit request UUID, with deterministic fallback for non-web
  callers, to exact decision material. Exact retransmission is idempotent;
  reusing a key for different material is a conflict.
- Create the response projection with no-clobber create-or-verify semantics so
  a post-commit projection failure can be recovered by exact replay.

**Implementation**

- `src/market_research/research/hash_chain.py` adds
  `mutate_hash_chained_jsonl_atomic`, which validates the old stream, stages
  sequential hashes, fsyncs a temporary complete stream, atomically replaces
  it, and fsyncs the parent directory while holding a POSIX lock.
- `src/market_research/research/governance.py` performs approval validation and
  the paired append within that mutation. It rejects late review after final
  approval, a prohibited actor, an outstanding change request, stale evidence,
  a conflicting request key, and orphan approval rows.
- `src/market_research/storage_io.py` adds durable parent-directory fsync and a
  bounded, no-follow, hard-link-based `write_json_atomic_create_or_verify`.
- The application and web governance adapters pass the request key and actor
  exclusions. The approval form carries a hidden UUID and exact POST replay is
  safe.

The guarantee is a single-host POSIX-filesystem evidence transaction, not a
database row lock or a distributed-filesystem consensus guarantee.

**Verification**

Component runs passed: root governance/storage/hash selectors (`47 passed`),
web governance selectors (`50 passed`), and application contracts (`9 passed`).
Tests cover different approvers racing, duplicate keys, same-key conflicting
material, originator/reviewer exclusion, lifecycle change, rollback before
publish, late review, orphan evidence, and projection replay. One integrated
CLI regression exposed an overly narrow review-state condition; restoring the
existing draft change-request behavior made the exact failing selector and two
adjacent race regressions pass (`3 passed`).

**Rediagnosis**

The canonical file evidence is now atomically decided and replay-safe on the
tested filesystem. Multi-host/NFS locking and an independently implemented DB
approval transaction remain unproven; PostgreSQL tests cover existing ORM
constraints and job claims, not this file-backed approval protocol.

## Current operational-hardening cycle — iteration 4/6: supported-database boundary

**Diagnosis**

The adapter defaulted to SQLite and accepted database configuration without a
strict operating contract. SQLite tests could not establish multi-user or
multi-worker guarantees, and no real PostgreSQL service was available locally.

**Design**

- Preserve SQLite as a development/test default, but refuse ambiguous or
  incomplete PostgreSQL settings.
- Keep database selection outside the common application layer.
- Add real-backend contract tests that skip visibly unless Django is connected
  to PostgreSQL; do not emulate PostgreSQL concurrency with SQLite.
- Supply the driver as an optional adapter dependency, not as a declaration of
  production support.

**Implementation**

- `apps/internal_web/src/market_research_web/database.py` builds strict SQLite
  or PostgreSQL settings, validates required fields, port and SSL mode, rejects
  unknown engines, and enables atomic requests.
- Django settings use that builder. The web package exposes an optional
  `postgresql` extra with Psycopg 3.
- `test_postgresql_integration_contract.py` exercises partial unique indexes,
  rollback, two independent job-claim sessions, and concurrent global web
  experiment-ID creation when a real PostgreSQL backend is present.

**Verification**

Database/configuration contracts passed locally. The integrated run reports
`3 skipped` PostgreSQL cases because the configured backend is SQLite; these
skips are absence of evidence, not passes. Lock consistency and migration drift
checks are recorded in iteration 6.

**Rediagnosis**

The application-side compatibility and fail-closed configuration boundary are
implemented. A supported-database claim still requires a provisioned
PostgreSQL instance, the documented test command passing with zero PostgreSQL
skips, migration rehearsal, backup/restore rehearsal, and operating ownership.

## Current operational-hardening cycle — iteration 5/6: cross-adapter identity and trust boundaries

**Diagnosis**

The web ORM uniqueness constraint covered only web-created IDs. CLI validation
could independently reuse an `experiment_id` for different manifest content.
Historical report search and reproduction also remained path-oriented and
could not safely be enabled for arbitrary artifacts.

**Design**

- Bind `research-validate` identity in one repository-external, hash-chained
  authority called by both CLI and web application entry points before engine
  execution. Split artifact/report mounts must provide its explicit common
  path; sibling roots derive one common-parent path.
- Treat the same ID plus manifest hash as idempotent and the same ID plus a
  different manifest hash as a fail-closed conflict under the registry lock.
- Preserve existing human-readable IDs and final-holdout registry semantics;
  do not silently scan/import legacy artifacts or add repair commands.
- Keep historical search and reproduction disabled until an authoritative
  ownership index, immutable code/data bindings, allow-listed execution, and
  external recovery gates exist.

**Implementation**

`src/market_research/research/experiment_identity.py` implements the shared
validation registry. Both application validation paths bind through it before
calling the engine. Storage and validation documentation describe the registry
scope and explicitly exclude standalone backtest, walk-forward, legacy
artifacts, final-holdout identity, principal ownership, and exclusive
execution. Architecture and operations documents record the catalog and
reproduction threat model and activation criteria.

**Verification**

Identity/application selectors passed (`22 passed`), including exact replay,
conflicting reuse, concurrent binding, corrupt-registry refusal, and CLI/web
competition. Existing report-catalog tests remain in the integrated web run.

**Rediagnosis**

New guarded validation runs share an ID owner. Legacy and standalone workflow
namespaces remain deliberately unreserved, and there is no import/repair path.
Arbitrary historical discovery and reproduction remain disabled rather than
being made path-addressable.

## Current operational-hardening cycle — iteration 6/6: operations handoff and integrated validation

**Diagnosis**

The remaining P0 capabilities require service and data-plane ownership that
this repository is explicitly prohibited from implementing. Documentation
needed an executable handoff instead of an implied production claim, and all
new boundaries needed integrated validation without repeating the previously
interrupted repository-wide suite.

**Design**

- Define liveness, readiness, diagnostics, TLS/proxy, persistent worker,
  supervision, logging, monitoring, backup fence, restore order, and release
  evidence as acceptance criteria owned outside this repository.
- Name the exact application primitives and validators the external system may
  call, while forbidding direct artifact mutation and invented repair paths.
- Re-run focused changed boundaries, migration/system/deployment checks,
  package lock checks, compile checks, a root build, and whitespace validation.

**Implementation**

- Added `docs/internal-web-operations-handoff.md` with required mounts, secret
  and database settings, projection result semantics, worker retry/lease/DLQ
  ownership, health meanings, backup/restore ordering, consistency validators,
  report/reproduction gates, and a release acceptance record.
- Updated `docs/internal-web-architecture.md`, `apps/internal_web/README.md`,
  the validation/storage documents, and this evidence log so implementation,
  integration surface, and prohibited operations work are clearly separated.
- Final review found and fixed three additional integrity gaps: durable JSONL
  append now flushes and fsyncs and rejects an unterminated final row; chain
  metadata and semantic rows are consumed from one locked generation; and
  split artifact/report mounts must name one common experiment-identity
  authority. The identity bind now validates semantics, resolves an exact
  replay or conflict, and publishes a new row in one atomic locked mutation.
- The operations contract records the full-stream cumulative quadratic cost,
  immutable anchored-segment acceptance criteria, and the external active
  namespace claim required to stop same-ID/same-manifest concurrent engines.

**Verification**

The integrated root changed-boundary invocation initially reported `65 passed,
1 failed`; the failure was the draft change-request regression described in
iteration 3. Its exact selector plus adjacent approval race tests then reported
`3 passed`. The integrated web invocation reported `89 passed, 3 skipped`; all
three skips require a real PostgreSQL backend. The architecture policy selector
reported `13 passed`. Django's test settings check reported no issues and
`makemigrations --check --dry-run` reported no changes. A production-shaped
deployment check reports only `security.W021` because HSTS preload remains an
external deployment decision; HSTS itself, secure redirect, trusted origin,
allowed host, and a strong injected secret passed their checks.

Root/web lock, compile, build, and final Git integrity results are recorded in
the final task report after their last execution. After the last integrity
changes, the final root changed-boundary invocation reported `84 passed`; the
final web invocation reported `91 passed, 3 skipped`, with all skips requiring
a real PostgreSQL backend. Root and web lock checks and compileall passed, the
root wheel and sdist built successfully, and `git diff --check` passed. Django
test settings and migration drift checks passed. The production-shaped deploy
check again reported only intentional `security.W021` for HSTS preload. The
interrupted full root suite was not repeated under the repository testing
policy.

**Rediagnosis**

All safe application-repository improvements identified in this cycle are
implemented. Remaining gaps require a real PostgreSQL environment, an
authorized operations repository, TLS and secret infrastructure, persistent
process supervision, monitoring, backup/restore rehearsal, and a durable
external admission claim preventing two identical validation calls from
writing one experiment namespace concurrently. Consequently, this adapter is
not a currently operable long-term internal service.

## 2026-07-16 continuation — sibling operations implementation and exact recovery

This section supersedes only the stale *current-state* conclusions above. The
earlier iteration entries remain an accurate record of what was known and
tested at those points in time. Operational implementation was kept out of this
repository and placed in the separately authorized sibling
`/home/vorac/work/ResearchOperations`.

### Recovered state and completed application boundaries

The interrupted worktree, unstaged and staged diffs, recent commit, migrations,
generated files, and incomplete commands were inspected without reset, revert,
checkout, stash, deletion, or staging. Existing changes were preserved.

The resumed implementation established PostgreSQL-authoritative web governance
state with locked lifecycle rows, unique duty and decision constraints,
idempotent operation IDs, originator/reviewer/approver separation, and an
atomic ORM mutation plus audit intent. Core governance JSONL and approval
artifacts remain independently hash-bound research evidence; the database and
filesystem are deliberately validated as two authorities rather than described
as one physical transaction. Portal migration `0007_governance_authority`
creates that authority.

Portal migration `0008_imported_decision_report` adds an admin-only historical
decision-report catalog. The approved input contract allows an authenticated
administrator to submit an absolute source path only beneath a server-configured
`INTERNAL_WEB_REPORT_IMPORT_ROOTS` entry. The adapter rejects symlink/traversal,
performs a bounded no-follow read, requires explicit report, manifest, dataset,
experiment, run and code-revision bindings, publishes a content-addressed
managed copy, and commits catalog metadata with an immutable audit intent.
Owner and organization visibility are enforced and the source path is not
retained or served. Arbitrary discovery and every web reproduction route remain
fail-closed.

The approval UI was brought into line with the implemented policy: the login
page no longer calls approval disabled, an approved candidate shows a terminal
success/exact-replay banner instead of live review and approval forms, and the
report catalog shows the import panel only when the import permission is held.

### Operations trust domain

The sibling operations schema now has three checked migrations:

- `0001_initial` supplies durable outbox delivery, worker heartbeats, experiment
  identity, request history, and one active fenced namespace claim;
- `0002_runtime_control` supplies admission fencing, validation observations,
  verified backup registration, and restore-drill evidence;
- `0003_research_job_receipt` supplies the fenced immutable publication receipt
  that reconciles the intentional admission-result/ResearchJob terminal-state
  transaction window without rerunning the research engine.

Persistent audit-delivery and research-job workers use PostgreSQL leases,
monotonic fencing, `FOR UPDATE SKIP LOCKED`, bounded retry and dead-letter
semantics, signal-aware drain, and immutable result validation. Guarded web and
operations WSGI surfaces implement fixed liveness, separate `web-read` and
`workflow-mutation` readiness, bounded authenticated diagnostics, and label-free
metrics. A two-phase backup fence keeps outbox claims open during `DRAINING` so
committed audit intents can drain, closes all claims only in `SEALED`, and binds
the PostgreSQL dump and role-separated filesystem archives in one detached-
signed manifest.

### Packaging failure and correction

An installed-wheel smoke test found a real deployment defect that editable
development installs had hidden: the internal-web wheel did not contain its
Django templates or static assets. It also inferred the Research project root
from installed module parents, which points into the installation environment
rather than the pinned Research source tree. The web distribution now declares
its template/static package data, and production-shaped settings require the
explicit absolute `RESEARCH_OPS_SOURCE_ROOT`. Wheel smoke tests then resolved
templates and collected static content successfully. The final release bundle
digest used by backup and recovery was
`sha256:547577d7239b5276cb35d42cbcf2c3bfcb57cb6ef72c073c81f2adc6d6b64674`.

### Actual PostgreSQL, worker, browser, and TLS evidence

Validation was performed against PostgreSQL 16.14 over TLS 1.3 with
`sslmode=verify-full`, not an SQLite substitute:

```text
Research complete suite:              596 passed
web complete PostgreSQL suite:        158 passed, 0 skipped
web required browser E2E:              1 passed
Operations complete PostgreSQL suite: 43 passed, 0 skipped
Operations live PostgreSQL tests:      16 included in the 43
```

All eight PostgreSQL-specific web concurrency and governance-atomicity tests
executed. Django system/deploy checks and migration drift passed; changed web
files passed Ruff, and the Operations project passed Ruff lint/format, shell
syntax, dependency-lock, compile, and sdist/wheel build checks. The browser test
closes thread-local Django connections before database teardown; this removed
the interrupted-session drop warning without weakening assertions.

Two audit-delivery workers and one research-job worker reported fresh
heartbeats. Stopping one delivery worker cleanly made workflow readiness fail
with the expected insufficient-worker reason; restarting it restored readiness.
One immutable audit intent passed through scan, lease, projection and marker
update with exactly one JSONL row.

Native nginx and Gunicorn acceptance passed actual bind and configuration
validation, HTTP-to-HTTPS `308`, valid CA/hostname TLS and HTTP/2, and negative
CA and hostname verification (both curl exit 60). HSTS, CSP, clickjacking and
content-type headers and static delivery passed. Employee ingress returned 404
for operations paths. The operations ingress rejected a missing client
certificate, required mTLS for liveness, required additional Basic authorization
for diagnostics and metrics, and returned both readiness policies as ready with
all 13 diagnostics checks passing. An actual HTTPS CSRF login set Secure,
HttpOnly and SameSite cookies as applicable, reached the dashboard and one
cataloged report, and redirected an unauthenticated catalog request.

This native run also confirmed two operational constraints: the audit validator
must remain persistent because workflow readiness becomes stale and fails after
its 300-second observation window, and a native WSL launch must put `TEMP` and
`TMP` on Linux storage rather than inherited DrvFS so heartbeat `os.utime()` is
reliable. The container reference uses tmpfs and does not inherit that desktop
path.

### Exact backup, blank restore, and resumed service

Backup `df9ac410-a085-452e-be09-50c27a312bee` was created from the exact final
bundle. Its six-role signed manifest passed independent verification:

```text
manifest hash: sha256:ec7ea9a51985e90696eb415478104afe2aaaac715007414c7c0a18e21ebf0fe4
audit rows:   2
audit hash:   sha256:3d48eca860529777e39d23590e2becb4279d867519f607739f8232792c58ed0e
```

A newly created empty PostgreSQL database and absent filesystem namespace were
restored without overlay or repair. The signed offline verifier returned PASS
for all 17 checks, including authentication, migration leaves, imported
reports, manifests, job receipts/results, canonical reports, governance,
experiment identity, segmented audit/outbox binding, closed admission, and zero
writer state. The receipt and deterministic control record were:

```text
receipt hash: sha256:b3cbfad45032debf4cab9d3a7768c309e0aa3d0259a3b9094783560d7efc814d
drill ID:     ec28ec13-f19d-5bc1-aa79-f1156d708ff1
```

Because restore intentionally uses `--no-owner --no-privileges`, the checked
least-privilege ACL migration/reapply step is a mandatory gate before service
startup; activation is not a substitute for it. After that gate,
`recovery-activate` verified the signed PASS receipt, the sealed fence binding
and zero-writer state, and opened admission at generation 4. Repeating the exact
activation returned the same manifest, receipt and drill binding with
`already_activated=true` rather than creating a second record.

The restored namespace started two delivery workers and one research-job
worker, refreshed the full validator, and reached both readiness policies. The
restored acceptance user completed CSRF login and retrieved the cataloged
report. Audit event `4a7c1f83-0b23-49dc-8071-51330a7ea76a` was scanned and
projected exactly once, producing one database marker and one segmented-stream
row. Pointing health at an unavailable database endpoint made readiness fail
closed with `database_unavailable`; restoring the valid DSN made it ready again.

Earlier failed backup and restore attempts were preserved as incident evidence.
They exposed and led to fixes for backup-role sequence `SELECT`, exact
`BACKUP_RESUME_ID` continuation, safe database-name quoting, isolated restore
resume, inherited read-only `PGOPTIONS` on the separate control connection,
the governance registry path, signed receipt reuse, deterministic drill IDs,
and explicit recovery activation.

### Final rediagnosis

The result supports a **limited single-host internal trial**. It does not support
the unqualified labels “production-ready”, “multi-host ready”, or “fully
operational long-term service”. Promotion remains blocked on immutable container
image execution on the selected host, site-issued certificate renewal/revocation
and no-drop reload, off-site/encrypted retention and approved RPO/RTO, scheduled
restore drills, prior-release upgrade rehearsal, multi-host and power-loss
filesystem qualification, named service/security/data owners and on-call, and a
deployment rule that prevents standalone Research CLI entrypoints from bypassing
Operations admission. Web reproduction remains disabled.
