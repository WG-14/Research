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
