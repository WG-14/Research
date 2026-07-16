# Internal Web Operations Handoff Contract

Status: **implemented for a limited single-host internal trial, not production-
accepted**. The separately authorized implementation is the sibling
`/home/vorac/work/ResearchOperations`. This document does not add or authorize a
health endpoint, retry command, worker daemon, deployment unit, reverse proxy,
monitoring agent, or backup/restore tool in the Research repository itself.

The internal web project remains an isolated Django adapter over offline
research application services. PostgreSQL coordination, persistent workers,
guarded probes, TLS/proxy configuration and recovery are implemented outside
this repository. The 2026-07-16 evidence below proves a recoverable single-host
acceptance namespace; it does not prove immutable production-image execution,
site PKI lifecycle, multi-host behavior, approved RPO/RTO, or operational
ownership. The unqualified labels “production-ready” and “long-term operated
service” therefore remain prohibited.

## 1. Repository ownership boundary

`market-research` owns research semantics and the following reusable
application primitives:

- typed application requests and permission checks;
- immutable manifest publication and bounded, hash-verified artifact reads;
- web job state transitions, claims, lease tokens, and idempotency constraints;
- database-backed audit intents and hash-chained JSONL projection;
- report, governance, registry, and audit integrity validators.

It deliberately does **not** own process supervision, deployment, service
coordination, health checks, operational state repair, retry scheduling,
monitoring delivery, or backup/restore. Those capabilities are forbidden by
this repository's `AGENTS.md`.

The existing sibling `Operation` repository is not the destination for this
work. It is a safety-first trading-operation runtime whose current change
contract is limited to lot-native execution declarations. Mixing a research
portal, its users, its PostgreSQL schema, or its evidence retention into that
runtime would combine independent trust domains and violate its active scope.

**Implemented ownership decision:** `ResearchOperations` is the distinct
operations trust domain. It consumes a pinned Research source/release and does
not make the trading `Operation` repository a dependency. Named maintainers,
security and evidence owners, on-call rotation, site release pipeline and
incident approvals remain promotion gates rather than inferred ownership.

## 2. Current persistence and consistency facts

The runtime must preserve the meanings of these settings and paths:

| Setting | Current use | Operational mount rule |
| --- | --- | --- |
| `RESEARCH_DATA_ROOT` | Externally prepared immutable datasets and content-addressed web manifests under `_internal_web/manifests/` | Read-only for prepared datasets; only the dedicated manifest subtree may be writable. Prefer a nested writable mount rather than write access to all datasets. |
| `RESEARCH_ARTIFACT_ROOT` | Derived evidence plus `_internal_web/` state, including the logical `audit/web_audit.jsonl` path and its complete `.segments/{checkpoint.json,segments,metadata,receipts}` tree, and the development SQLite metadata database | Shared read/write POSIX storage for web and workers. Multi-host acceptance requires demonstrated atomic rename, append, `fsync`, and `fcntl` lock behavior. |
| `RESEARCH_REPORT_ROOT` | Canonical reports and web result projections | Shared read/write storage; never served directly by the proxy. Downloads must continue through bounded, hash-verifying application code. |
| `RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH` | Shared `research-validate` experiment-ID to manifest-hash authority | Required and identical in every CLI/web process when artifact and report roots are separate mounts; shared read/write POSIX storage, included in the recovery set. Sibling roots may use the derived common-parent registry. |
| `RESEARCH_CACHE_ROOT` | Rebuildable cache entries | Read/write storage with a separately documented eviction policy. It must never be the only copy of evidence. |
| `RESEARCH_DB_PATH` | Optional immutable research input SQLite file | Read-only input. It is not the Django metadata database and must not be treated as one. |

Every configured root and `RESEARCH_DB_PATH` must remain absolute and outside
the Git checkout. A release must fail before accepting traffic if a root is
missing, resolves inside the checkout, resolves through an unexpected symlink,
or has broader permissions than its declared mount rule.

SQLite remains a local-development compatibility profile. In that profile the
Django metadata file is
`RESEARCH_ARTIFACT_ROOT/_internal_web/operations.sqlite3`. It is not an
accepted multi-user, multi-process, shared-filesystem, or failover database.

The experiment-identity authority enforces one manifest hash for an ID; it is
not by itself an execution mutex. `ResearchOperations` now supplies the durable
active namespace claim keyed by authority plus experiment ID, idempotent request
history, lease and monotonic fencing, and result publication binding. Exact
admitted retries converge and a stale token cannot publish. This protection
applies only when web or CLI work enters through Operations admission.
Standalone Research CLI entrypoints can bypass it and must be excluded by the
deployment policy; the identity file alone does not make such calls safe.

### 2.1 Audit outbox consistency

For each portal mutation that calls `record_web_audit_event`, the ORM mutation
and one immutable `WebAuditEvent` intent commit in the same database
transaction. This does not imply that Django session/admin internals are
research audit events. An `on_commit` callback then projects the payload to
`RESEARCH_ARTIFACT_ROOT/_internal_web/audit/web_audit.jsonl` and marks the row
with the projected JSONL row hash. Consequently:

1. committed ORM state always has a committed audit intent;
2. the JSONL append occurs after the database commit, not atomically with it;
3. a crash or I/O error can leave either a pending intent, or a JSONL row whose
   database marker was not written;
4. the state is eventually consistent only while a delivery process is
   functioning; the sibling Operations project supplies and supervises that
   persistent process, while this repository intentionally does not;
5. direct, audit-only events can be appended without an outbox row and are not
   outbox orphans.

The hash-chain append primitive uses `event_id` as an idempotency identity while
holding the stream lock. The same ID and identical immutable payload return the
existing row; the same ID with different material, multiple matching rows, or
an invalid chain fail closed. This permits safe adoption of the
append-before-marker gap. It does not make the database/filesystem write an
atomic transaction, and it is not a retry scheduler.

The low-level JSONL append writes one newline-terminated record, flushes and
`fsync`s the file before returning, and `fsync`s the parent directory when the
stream is first created. A nonempty stream without its final newline is treated
as interrupted even when its last bytes happen to form valid JSON; delivery
stops and validation fails rather than adopting that row. These calls still
require qualification on the selected POSIX filesystem and mount under process-
kill and power-loss tests. No startup routine may truncate or repair a partial
line automatically.

`portal.audit.validate_web_audit_outbox()` is the current in-process integrity
check. It reports the chain result plus pending, duplicate, orphan, unmarked,
missing, payload-mismatch, intent-hash, and projection-hash failures. Any
reported reason makes its status `FAIL`. It is a Python validation API, not an
HTTP health endpoint or an operator command.

### 2.2 Operations audit delivery and research-job workers

The Operations implementation delivers pending audit intents under the
following contract:

- `portal.audit.project_web_audit_event()` is the reviewed single-event
  integration primitive. It combines intent validation, idempotent JSONL
  append/adoption, compare-and-set marker update, and revalidation of an
  already marked row. It accepts only an outbox UUID, accepts no path or payload
  override, and returns only the UUID, projected row hash, and `PROJECTED` or
  `ALREADY_MARKED`. It does not discover or schedule work.
- The worker treats `WebAuditEvent.payload` and `payload_hash` as immutable and
  never deletes, rewrites, or fabricates an intent. It may update only the
  delivery marker through the reviewed projection primitive.
- Scheduling metadata is operations-owned and keyed by the outbox event UUID.
  The web model has no delivery lease, attempt, or dead-letter fields.
  `research_ops.outbox_delivery` stores lease ownership, attempt count, bounded
  exponential backoff with jitter, next-attempt time, sanitized error category,
  and dead-letter state outside research evidence.
- Multiple delivery workers claim operations-owned schedule rows atomically.
  A lost lease cannot mark delivery complete. Duplicate execution is expected
  and must converge through the event-ID idempotency primitive.
- Transient filesystem/database availability failures are retried within a
  documented maximum lag and attempt policy. Hash conflicts, malformed JSONL,
  duplicate event IDs, payload mismatch, or chain validation failure are
  permanent integrity incidents: stop projection, dead-letter the scheduling
  record, keep the original intent untouched, and alert.
- Dead-lettering never makes the outbox validator pass. It is an operational
  escalation state, not evidence repair.
- The JSONL path and its `.lock` file are on one Linux POSIX filesystem visible
  to every delivery process. The exact filesystem and mount options must pass
  cross-host lock and crash tests; assuming generic NFS lock behavior is not
  sufficient.
- On success, the worker reruns a bounded event-level binding check. A scheduled
  full-stream validation also runs independently. Its sanitized aggregate
  result and terminal stream hash are retained in operations telemetry.

The production audit profile now uses immutable bounded segments, a checkpoint,
metadata, per-event receipts and prior-terminal-hash bindings. Projection and
load-path checks use bounded incremental validation; an independent scheduled
full reconciliation reads every retained segment and detects sealed-segment
corruption. Plain rotation, truncation, or an unbound index remains forbidden.
Row/byte, rollover and lock-latency SLOs are still site policy inputs.

The delivery loop discovers pending UUIDs from PostgreSQL in bounded
`created_at, id` order, upserts each into its operations-owned schedule table
under a unique UUID constraint, claims that schedule row with a lease, invokes
the public projection function, confirms the database marker and returned hash,
and then completes the schedule row. Discovery is repeatable and may race;
neither discovery nor lease loss changes the immutable intent. An unmarked
existing JSONL row follows the same loop and returns `PROJECTED` after adopting
the row rather than appending a second row.

Acceptance tests must inject a failure before append, after append but before
the database marker, during concurrent duplicate delivery, after a worker lease
loss, on malformed/truncated JSONL, and on a conflicting payload. The first
four cases must converge to exactly one JSONL row and one matching marker. The
integrity-conflict cases must remain fail-closed without automatic repair.

The persistent Operations research-job worker wraps the reviewed one-job web
dispatcher. It acquires both the ResearchJob and experiment admission leases,
heartbeats and fences both, and verifies the immutable result before publication.
Operations migration `0003_research_job_receipt` makes admission completion and
the immutable result receipt one transaction. Applying the receipt to the
ResearchJob is intentionally a second transaction; a crash in that window is
reconciled from the receipt without rerunning the engine, and an unapplied
receipt blocks readiness and backup sealing. An expired `RUNNING` job remains
evidence and is never generically requeued or overwritten.

### 2.3 Database-authoritative governance and report catalog

Portal migration `0007_governance_authority` makes PostgreSQL authoritative for
web governance subject lifecycle, actor duty claims and idempotent decisions.
Row locks and unique constraints independently enforce originator/reviewer/
approver separation and one final approval while core governance JSONL and the
approval artifact remain hash-bound research evidence. Recovery verifies both;
it does not pretend their database and filesystem writes are one physical commit.

Portal migration `0008_imported_decision_report` adds the owner/organization-
scoped managed historical report catalog and grants its explicit permission to
the research administrator group. The exact import contract is in section 7.
Operations migrations `0001_initial`, `0002_runtime_control`, and
`0003_research_job_receipt` are checksum-bound migration leaves and are included
in readiness, backup and recovery verification.

## 3. Supported database gate

The adapter contains a strict PostgreSQL connection-settings boundary and the
operations bundle pins Psycopg. Selecting the profile still does not provision a
server or apply migrations. The 2026-07-16 single-host acceptance run did apply
the complete Django and Operations migration leaves and execute the live gates
described below against PostgreSQL 16.14 over TLS 1.3 `verify-full`.

The external runtime must supply these exact application settings:

```text
INTERNAL_WEB_DATABASE_ENGINE=postgresql
INTERNAL_WEB_DATABASE_NAME=<database name>
INTERNAL_WEB_DATABASE_USER=<least-privilege runtime role>
INTERNAL_WEB_DATABASE_PASSWORD=<secret injection>
INTERNAL_WEB_DATABASE_HOST=<verified DNS name>
INTERNAL_WEB_DATABASE_PORT=<1..65535>
INTERNAL_WEB_DATABASE_SSLMODE=verify-full
INTERNAL_WEB_DATABASE_SSLROOTCERT=<absolute approved CA path>
INTERNAL_WEB_DATABASE_CONNECT_TIMEOUT_SECONDS=<bounded seconds>
INTERNAL_WEB_DATABASE_STATEMENT_TIMEOUT_MS=<bounded milliseconds>
INTERNAL_WEB_DATABASE_LOCK_TIMEOUT_MS=<bounded milliseconds>
INTERNAL_WEB_DATABASE_IDLE_TRANSACTION_TIMEOUT_MS=<bounded milliseconds>
INTERNAL_WEB_DATABASE_CONN_MAX_AGE_SECONDS=<bounded seconds>
INTERNAL_WEB_DATABASE_APPLICATION_NAME=<allowlisted identifier>
```

The settings builder also recognizes other libpq SSL modes and defaults to
`require`; neither behavior is sufficient for operational acceptance. The
Operations bundle must run `uv sync --frozen --no-dev --no-editable` from its
pinned lock so installed distributions, not editable source shortcuts, are
tested and browser/development dependencies are absent. It must explicitly
select `verify-full`, install the approved CA chain through a libpq-supported
secret/file mechanism, and pass valid-hostname, wrong-hostname,
expired-certificate, and untrusted-CA tests. The operations build owns the
pinned Python/PostgreSQL driver and its security updates.

Additional database acceptance criteria:

- choose and record one supported PostgreSQL major version and patch policy;
- use a dedicated migration role and a lower-privilege application role;
- run exactly one externally coordinated migration job before new processes
  receive traffic; do not run `migrate` in every web/worker startup;
- prove fresh installation and upgrade from every supported prior release, and
  prove `makemigrations --check --dry-run` has no drift;
- set and test connection, statement, lock, and idle-in-transaction timeouts;
- calculate web/worker connection limits against the server limit and reserve
  capacity for migration, diagnostics, and recovery;
- keep primary-consistent reads for job, permission, approval, and outbox
  decisions; a lagging replica cannot serve those paths;
- record the transaction isolation level used by the test and runtime profiles.

At least the following tests must run against a real, separately provisioned
PostgreSQL database, using multiple OS processes and synchronization barriers
rather than only SQLite or thread-local test transactions:

1. simultaneous registration of one `experiment_id` permits exactly one
   authoritative web manifest;
2. simultaneous identical enqueue requests converge on one job, reuse of an
   idempotency key for different material fails, and different active requests
   cannot bypass the one-active-job constraint;
3. multiple workers claim each queued job at most once and a stale lease token
   cannot report progress or complete it;
4. completion versus cancellation produces one allowed terminal outcome;
5. login-throttle updates cannot lose failures or bypass a block window;
6. outbox intent commit, rollback, concurrent projection marking, and validator
   reads preserve their documented bindings;
7. concurrent final approvals produce one authoritative approval result, and
   an exact request replay has the documented idempotent outcome;
8. a process kill at every documented database/filesystem boundary leaves a
   detectable state that the validator classifies consistently.

Passing settings-unit tests or Django's SQLite suite does not satisfy this
gate. The actual live-PostgreSQL web suite completed with `158 passed,
0 skipped`, including all eight PostgreSQL-specific concurrency and governance-
atomicity tests. The Operations suite completed with `43 passed, 0 skipped`,
including 16 live PostgreSQL tests for migrations, SKIP LOCKED claims, leases,
fencing, worker drain, admission and result-receipt reconciliation. This closes
the prior “PostgreSQL unavailable” gap for one host. It does not by itself prove
every process-kill point, upgrade from every prior release, failover, capacity,
patch policy, or multi-host filesystem behavior; those remain release-record
items.

## 4. Liveness, readiness, and diagnostics contract

There are no health or readiness routes in the Django URL configuration, and
they must not be added to this repository under the current boundary. The
Operations WSGI surface implements the probes with these distinct meanings:

### Liveness

Liveness answers only whether the specific process can execute its request
loop. It must not query PostgreSQL, walk a filesystem, scan JSONL, inspect a
worker, or mutate state. A failure asks the supervisor to replace that process;
success does not mean the application is safe to receive research work.

The unauthenticated response is constant and bounded: HTTP 200/503 and a fixed
status token only. It must not expose release paths, hostnames, process IDs,
environment values, dependency versions, exception text, or timing details.

### Readiness

Readiness answers whether this release may receive its declared traffic. It
must return 503 and be removed from routing when any required condition is
false:

- release checksum and schema-migration leaves match the approved release;
- PostgreSQL accepts a bounded primary transaction, reports that it is not in
  read-only mode, completes a constant query, and rolls back without a durable
  write; no migration is pending;
- all required roots are mounted with the declared read/write policy;
- the most recent scheduled audit/outbox validation is within its maximum age,
  has no integrity reasons, and audit delivery lag is within the approved SLO;
- the required research-job and audit-delivery worker pools have a fresh
  operations-owned heartbeat and are accepting new claims;
- no deployment, backup fence, restore validation, or integrity quarantine is
  holding mutation admission closed.

The probe must use cached, bounded operations observations. It must not run a
full audit or report scan on each load-balancer request. If read-only access is
kept available during a worker outage, expose separate `web-read` and
`workflow-mutation` readiness policies; the state-changing routes must remain
closed when evidence delivery or queue execution is outside policy.

### Diagnostics

Diagnostics is authenticated, authorization-checked, rate-limited, audited,
and unavailable through the general employee ingress. It returns only stable
reason codes, bounded counts, observation times, opaque release IDs, and
correlation IDs. It may summarize database connectivity, migration status,
root mount roles, worker freshness, queued/running/expired counts, oldest
outbox lag, audit validator status/counts, latest backup age, and latest restore
drill result.

It must never return secret values, cookies, session identifiers, credentials,
CA material, raw SQL, exception text, environment dumps, absolute paths, audit
payloads, manifest bodies, report bodies, or usernames. Existing validators
that return paths (for example governance validation) require an explicit
redacting projection before use in diagnostics.

Probe tests must demonstrate constant unauthenticated output, method and size
limits, timeout behavior for each dependency, no mutation, no secret/path
leakage, and readiness transitions for database, mount, worker, outbox, backup
fence, and restore-quarantine failures.

The externally implemented probe surface is fixed as follows; it is not part of
the current Django URL configuration:

| Method and path | Authentication | Response contract |
| --- | --- | --- |
| `GET /__ops/live` | Network-restricted probe identity; no user session | 200 `{"status":"UP"}` or 503 `{"status":"DOWN"}` only. |
| `GET /__ops/ready/web-read` | Network-restricted probe identity; no user session | 200 `{"status":"READY"}` or 503 `{"status":"NOT_READY"}` only. |
| `GET /__ops/ready/workflow-mutation` | Network-restricted probe identity; no user session | Same fixed body; includes worker and evidence-delivery gates. |
| `GET /__ops/diagnostics` | Mutually authenticated operations identity plus diagnostics authorization | Schema-versioned, bounded check summaries with stable codes and counts; never raw details. |
| `GET /__ops/metrics` | Mutually authenticated operations identity plus diagnostics authorization | Label-free, allowlisted Prometheus snapshot; never paths, identities, payload labels, or exception text. |

All other methods return a fixed 405 response. General employee ingress must
not route `__ops` paths. Diagnostics schema version 1 contains only
`schema_version`, aggregate `status`, `observed_at`, generated
`correlation_id`, and a bounded `checks` array. Each check contains only an
allow-listed ID, `PASS`/`FAIL`/`STALE`, stable reason code, observation time,
and an optional nonnegative count.

## 5. TLS, proxy, secrets, and process lifecycle

The operations repository owns the following deployment acceptance criteria.
No example development server command satisfies them.

### TLS and reverse proxy

- Terminate only approved TLS versions and ciphers, automate certificate
  renewal, alert before expiry, and test renewal without dropping in-flight
  requests.
- Use exact `INTERNAL_WEB_ALLOWED_HOSTS` and HTTPS-only
  `INTERNAL_WEB_CSRF_TRUSTED_ORIGINS`; wildcards are not accepted.
- Keep `INTERNAL_WEB_SECURE_SSL_REDIRECT=true` and
  `INTERNAL_WEB_SECURE_COOKIES=true`.
- Set `INTERNAL_WEB_TRUST_X_FORWARDED_PROTO=true` only when the application is
  reachable exclusively through a proxy that removes any client-supplied
  forwarding header and writes the trusted value itself.
- Preserve CSP, clickjacking, content-type, referrer, permissions, COOP/CORP,
  HSTS, CSRF, and no-store response headers. HSTS duration changes require a
  rollback plan; preload is not implied.
- Do not map data, artifact, report, cache, media, or audit directories into a
  static file location.

The approved release configuration manifest must enumerate, without recording
secret values, every effective setting in these groups:

- roots and research policy: `RESEARCH_DATA_ROOT`, `RESEARCH_ARTIFACT_ROOT`,
  `RESEARCH_REPORT_ROOT`, `RESEARCH_CACHE_ROOT`,
  `RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH`, optional `RESEARCH_DB_PATH`,
  `RESEARCH_MAX_WORKERS`, `RESEARCH_RANDOM_SEED`, explicit absolute
  `RESEARCH_OPS_SOURCE_ROOT`, `INTERNAL_WEB_STATIC_ROOT`, and
  `INTERNAL_WEB_AUDIT_SEGMENT_ROWS`;
- database: all `INTERNAL_WEB_DATABASE_*` names listed in section 3;
- HTTP trust: `INTERNAL_WEB_ALLOWED_HOSTS`,
  `INTERNAL_WEB_CSRF_TRUSTED_ORIGINS`,
  `INTERNAL_WEB_TRUST_X_FORWARDED_PROTO`,
  `INTERNAL_WEB_SECURE_SSL_REDIRECT`, `INTERNAL_WEB_SECURE_COOKIES`,
  `INTERNAL_WEB_HSTS_SECONDS`, and
  `INTERNAL_WEB_HSTS_INCLUDE_SUBDOMAINS`;
- bounded policy: `INTERNAL_WEB_MAX_PARAMETER_CANDIDATES`,
  `INTERNAL_WEB_MAX_EXECUTION_SCENARIOS`, `INTERNAL_WEB_MAX_WORK_UNITS`,
  `INTERNAL_WEB_LOGIN_FAILURE_LIMIT`,
  `INTERNAL_WEB_LOGIN_FAILURE_WINDOW_SECONDS`, and
  `INTERNAL_WEB_LOGIN_BLOCK_SECONDS`;
- identity/time: the presence and secret version of
  `INTERNAL_WEB_SECRET_KEY`, plus `INTERNAL_WEB_TIME_ZONE`.

The manifest records a hash or secret version for secret values, never their
contents. `INTERNAL_WEB_JOB_LEASE_SECONDS` is currently a code setting of 120
seconds rather than an environment setting; supervisors must not pretend to
override it. Hard-coded upload/result byte limits are likewise release
properties and must be captured from the tested release.

An installed-distribution smoke test must also prove that the internal-web wheel
contains its `portal` and registration templates and its static assets. Editable
development imports are not evidence for this gate. The acceptance run found
and corrected missing package-data declarations and rejected installed-module
parent inference in favor of explicit `RESEARCH_OPS_SOURCE_ROOT` before the
final bundle digest was recorded.

#### 2026-07-16 native TLS acceptance evidence

Native nginx and Gunicorn were bound on isolated acceptance ports and passed
`nginx -t`, HTTP-to-HTTPS `308` with the exact location, valid CA/hostname TLS
and HTTP/2, and negative wrong-CA and wrong-hostname checks (curl exit 60).
HSTS, CSP, `X-Frame-Options: DENY`, `nosniff`, and static delivery passed.
Employee ingress returned 404 for both `/__ops` and `/_internal`. Operations
ingress rejected a missing client certificate, allowed mTLS liveness, returned
401 for missing or wrong Basic authorization on diagnostics, and returned 200
for authenticated diagnostics and metrics.

With a fresh full audit observation, `web-read` and `workflow-mutation` both
returned READY and all 13 diagnostic checks passed. An actual HTTPS login
exercised CSRF and produced Secure/SameSite cookies and an HttpOnly secure
session, then loaded the dashboard and one cataloged report; an unauthenticated
catalog request redirected to login. This used a short-lived test PKI. It does
not satisfy production renewal, expiry alerting, emergency revocation, or
no-drop reload. It also demonstrated that the validator must remain persistent:
after its 300-second evidence window expired, workflow readiness intentionally
returned NOT_READY.

### Secrets

- Inject `INTERNAL_WEB_SECRET_KEY`, the PostgreSQL password, TLS private keys,
  and CA trust through an approved secrets service or read-only secret mount.
  Do not place them in Git, container layers, process arguments, diagnostic
  output, or general logs.
- Document rotation owner, maximum age, emergency revocation, and the session
  impact of rotating the Django secret key.
- Separate runtime, migration, backup, and diagnostics credentials. Test that
  each role is denied the others' privileges.
- Mask secret-bearing environment variables in crash reports and support
  bundles. A configuration dump must use an allow-list, not name-based
  redaction alone.

### Web and worker supervision

- Pin a production WSGI/ASGI server and every runtime dependency; the Django
  development server is prohibited.
- Set explicit web process/thread counts, request/body/header/time limits, and
  graceful termination deadlines. Long research execution stays outside HTTP
  processes.
- Supervise research-job and audit-delivery worker pools independently. Bound
  their concurrency by measured CPU, memory, database connection, and shared
  storage capacity.
- On deployment, close mutation readiness and stop new claims first, then drain
  HTTP requests and workers. A worker may finish only while its lease is valid.
  If the deadline forces termination, preserve the `RUNNING` evidence and
  alert; do not silently requeue it on startup.
- Start the new release in this order: migration gate, offline integrity checks,
  audit delivery, research workers, mutation readiness, then general traffic.
  Rollback is allowed only when database and artifact schema compatibility is
  explicitly proven.

### Structured logging and correlation

Emit one structured JSON object per operational log event with a UTC timestamp,
severity, service/process role, opaque release ID, stable event/error code,
correlation ID, and, where relevant, opaque job ID, capability ID, outcome, and
duration. Do not log request bodies, form values, password confirmation,
cookies, CSRF tokens, database DSNs, environment dumps, absolute paths, raw
audit details, or raw research artifacts.

The current middleware creates a new UUID for each request, stores it on the
request, returns it as `X-Correlation-ID`, and persists it with queued job
evidence. It does not trust an inbound correlation header. The proxy must log
the application response correlation ID, or a future reviewed change must add
strictly validated trace-parent adoption. Do not claim end-to-end correlation
by forwarding an untrusted client value.

Log transport must provide bounded buffering, rotation/retention, backpressure
behavior, access controls, clock synchronization monitoring, and alerts for
loss. Logging failure must not leak data into HTTP responses or mutate research
outcomes.

## 6. Backup and recovery acceptance contract

Operational backup/restore is forbidden in the Research repository but is now
implemented in `ResearchOperations`. A recoverable service needs one coherent
recovery set containing:

- a PostgreSQL backup/PITR position for Django metadata and audit intents;
- immutable web manifests and their enclosing `RESEARCH_DATA_ROOT` evidence;
- `RESEARCH_ARTIFACT_ROOT`, including the web audit stream and governance and
  experiment registries;
- `RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH` when it is not already contained
  in an included common state bundle;
- `RESEARCH_REPORT_ROOT` and every canonical report referenced by metadata;
- the exact application release, migration leaves, dependency lock/build
  digest, root-role map, and encryption-key identifiers.

`RESEARCH_CACHE_ROOT` may be omitted only after a test proves every omitted
entry is safely rebuildable and is not referenced as authoritative evidence.
The immutable input identified by `RESEARCH_DB_PATH`, and every external
dataset it represents, must have its own content-hash-bound retention contract.

### Consistent backup fence

An accepted backup uses this two-phase quiescence:

1. acquire the exclusive advisory fence, close mutation readiness and new
   experiment/job admission, but keep outbox claims open in `DRAINING` so
   already committed audit intents can finish;
2. drain HTTP mutations and active writers without changing their outcomes and
   require active jobs, experiment claims, outbox delivery, unprojected intents,
   and unapplied job receipts all to reach zero;
3. record a fresh full audit observation and require
   `validate_web_audit_outbox()` to return `PASS` with zero pending, duplicate,
   orphan, and unmarked projections;
4. enter `SEALED`, which closes outbox and job claims as well as mutations;
5. record the audit terminal stream hash/row count, database backup/PITR
   position, migration leaves, release ID, and filesystem snapshot IDs in one
   signed backup manifest;
6. take the database and filesystem snapshots while the fence remains closed;
7. verify snapshot checksums before reopening claims and mutation readiness.

A failed attempt leaves its private fence receipt, staging evidence, and closed
admission intact. `BACKUP_RESUME_ID` may continue only that exact backup while
the database remains `DRAINING` or `SEALED`; it cannot create a replacement
fence or bypass verification.

Copying the SQLite file, PostgreSQL data directory, or JSONL file while writers
are active is not a backup. Independently timed database and filesystem backups
without the common fence and manifest are not one recoverable set.

### Restore order and fail-closed behavior

Restore into a new isolated namespace; never overlay the only running copy.

1. verify backup-manifest signature, release/build digest, snapshot identities,
   checksums, declared PostgreSQL version and any site-required encryption;
2. restore PostgreSQL to a newly created empty target and extract immutable
   data/manifests, artifacts/audit/registries, reports, and any retained cache
   into a previously absent namespace, with all application processes stopped;
3. start a version-pinned offline validation process, not the web or workers;
4. verify migration leaves, database constraints, root containment, web
   manifest hashes, referenced result artifacts, canonical report hashes,
   audit chain/outbox bindings, governance registry, experiment registry, and
   backup terminal hashes;
5. keep all readiness false on any missing object, hash mismatch, pending or
   orphan audit binding, schema drift, partial root, or unrecognized legacy
   evidence. Do not regenerate, truncate, delete, or mark evidence to make the
   restore pass;
6. because the dump deliberately omits owners and privileges, reapply and verify
   the checked least-privilege ACLs before any service role starts;
7. verify the signed PASS receipt again during explicit `recovery-activate`,
   bind the restored sealed fence and zero-writer state, then start audit
   delivery, research workers and web, enabling mutation traffic last.

The Operations recovery verifier imports these existing Python validation
surfaces in a version-pinned offline process:

- `portal.audit.validate_web_audit_outbox()`;
- `portal.storage.verify_result_artifact()` for each referenced web result;
- `market_research.research.governance.validate_governance_registry()`;
- `market_research.research.experiment_identity.validate_experiment_identity_registry()`;
- `market_research.research.research_decision_report.validate_research_decision_report()`;
- the experiment-registry binding validators for every retained experiment.

The Operations recovery orchestrator now enumerates authoritative database
references, applies bounded reads, validates the segmented stream and backup
terminal watermark, verifies imported reports, authentication and writer state,
redacts results, emits one detached-signed aggregate receipt, and exits nonzero
on unknown, skipped, missing, pending, orphaned or mismatched evidence. Exact
resume may reuse only a complete matching receipt/signature pair and converges
on a deterministic control drill ID. `recovery-activate` reruns the full verifier
while the target is `SEALED`; an exact retry after `OPEN` verifies the signed
binding and returns `already_activated=true`. There is no repair mode.

The final acceptance set used bundle digest
`sha256:547577d7239b5276cb35d42cbcf2c3bfcb57cb6ef72c073c81f2adc6d6b64674`,
backup `df9ac410-a085-452e-be09-50c27a312bee`, and manifest
`sha256:ec7ea9a51985e90696eb415478104afe2aaaac715007414c7c0a18e21ebf0fe4`.
A blank database and absent namespace passed all 17 offline checks. Receipt
`sha256:b3cbfad45032debf4cab9d3a7768c309e0aa3d0259a3b9094783560d7efc814d`
was registered as drill `ec28ec13-f19d-5bc1-aa79-f1156d708ff1`; activation
opened generation 4 and exact retry converged on the same evidence. After the
mandatory ACL reapply gate, the restored namespace reached readiness with two
delivery workers and one research-job worker, completed login and report access,
and projected audit event `4a7c1f83-0b23-49dc-8071-51330a7ea76a` exactly once.
Readiness failed closed with `database_unavailable` against a dead endpoint and
recovered when the valid database endpoint was restored.

The service owner must approve measurable RPO, RTO, retention, encryption,
off-site separation, deletion/legal-hold, and restore-drill frequency. A backup
job's success is insufficient; acceptance requires a scheduled isolated restore
drill that reaches the same validation gates and records its duration and
result.

## 7. Historical CLI report and reproduction threat model

The web report catalog is intentionally narrow. It derives authority from
visible completed validation jobs or an explicit `ImportedDecisionReport` row,
uses opaque IDs derived from verified hashes, and revalidates the bounded
managed copy and canonical decision report on every list, load, comparison and
download. It never scans an import root or discovers arbitrary files.

Unimported historical CLI artifacts have no web ownership/visibility row and
may contain absolute paths or other local topology. A path-oriented
reproduction request also creates a confused-deputy and mutation surface.
Relevant threats include:

- path traversal, symlink/TOCTOU escape, arbitrary-file probing, and existence
  enumeration;
- oversized/malformed JSON, unsupported legacy schemas, missing hash bindings,
  mutable datasets, or tampered receipts;
- cross-user disclosure because a CLI artifact has no authenticated owner or
  access-control list;
- absolute path, environment, command, or infrastructure disclosure through a
  raw report or diagnostic;
- command/subprocess injection if a browser request is translated into CLI
  arguments;
- collision with an existing experiment/output namespace;
- replay that changes holdout-attempt/registry evidence, consumes excessive
  resources, or overwrites authoritative artifacts;
- concurrent duplicate reproduction with ambiguous idempotency or visibility.

Historical decision-report import is implemented under this explicitly approved
contract:

1. Only an authenticated actor with `portal.import_research_report` may use the
   form. The actor may submit an absolute host path, but it must be strictly
   below one of the server-configured `INTERNAL_WEB_REPORT_IMPORT_ROOTS`; a
   filesystem root, relative path, traversal and symlink component are rejected.
2. The server performs a bounded no-follow read and verifies the supported
   decision-report schema, report hash, manifest, dataset snapshot/hash,
   experiment, run and code-revision bindings supplied by the administrator.
3. The original path is discarded after verification. A content-addressed copy
   is atomically created beneath the managed report root, and only its safe
   managed reference is stored.
4. The immutable catalog row records opaque report identity, owner, OWNER or
   ORGANIZATION visibility, hashes and evidence bindings. Catalog mutation and
   its audit intent share one PostgreSQL transaction; JSONL projection remains
   the normal detectable outbox boundary.
5. Exact duplicate import converges. A changed owner/binding, experiment/run
   collision, tampered managed copy, over-size object, unsupported evidence, or
   audit-intent failure is rejected without exposing the source path.
6. Reads recheck the managed content and bindings and enforce visibility. Tests
   cover authorization, cross-user visibility, tamper, traversal, symlink,
   bounded read, exact replay, race/rollback and source removal.

This is an intentional reviewed deviation from the earlier proposal to accept
only an operations object ID: an administrator-supplied path is allowed solely
as a selector inside fixed roots and is neither retained nor returned. It does
not authorize arbitrary path browsing, bulk discovery, schema translation, or
execution.

Reproduction remains disabled until all catalog gates pass and an additional
typed application workflow provides:

- only an opaque catalog ID plus expected hashes as input; no raw path, command,
  environment, output path, or arbitrary option field;
- administrator-only authorization, step-up confirmation, explicit purpose,
  immutable actor snapshot, and audit intent;
- a mandatory dry preflight that verifies the reproduction receipt, immutable
  dataset availability, supported schema, resource limits, and collision-free
  new output identity;
- a durable idempotency key and one authoritative result for concurrent exact
  replays;
- execution through the application service and supervised job boundary, never
  a shell-built CLI command;
- a new content-addressed output namespace that cannot overwrite the source or
  any existing CLI/web experiment;
- post-run comparison of source and reproduced evidence with a bounded,
  path-redacted projection; drift stays a research result, not an automatic
  repair action.

The limited import and catalog administration workflow is implemented in the
isolated web adapter because its PostgreSQL permission, owner/visibility and
audit transaction are application concerns. Reproduction supervision would be
an operator concern but is not implemented. Historical bulk discovery and web
reproduction remain unavailable rather than exposing a partial execution path.

## 8. Operational release acceptance record

Every promoted release must attach one immutable acceptance record containing:

- Research source commit and signed operations build digest;
- supported PostgreSQL/driver/filesystem/proxy/process-server versions;
- migration-from-prior and fresh-install results;
- the real-PostgreSQL concurrency test results listed in section 3;
- single-host filesystem qualification, and cross-host lock, atomic-write and
  crash-injection results only when the declared deployment scope is multi-host;
- audit delivery lag, duplicate-delivery, DLQ, and integrity-failure results;
- TLS, proxy-header, cookie, CSRF, security-header, secret-rotation, and
  authorization test results;
- liveness/readiness/diagnostics negative-test results;
- backup set verification and the latest isolated restore drill receipt;
- historical catalog/reproduction gate status; the current value is
  `IMPORT_ENABLED_REPRODUCTION_DISABLED`;
- named service owner, security owner, data/evidence owner, on-call rotation,
  approved RPO/RTO, and residual-risk approvals.

The current immutable acceptance evidence includes Research `596 passed`, web
PostgreSQL `158 passed, 0 skipped`, the required browser E2E, Operations
`43 passed, 0 skipped`, native TLS/proxy/CSRF/mTLS checks, build digest
`sha256:547577d7239b5276cb35d42cbcf2c3bfcb57cb6ef72c073c81f2adc6d6b64674`,
and the signed blank-restore drill recorded in section 6. It also includes
restored worker readiness, login/report access, exactly-once outbox projection,
and database-down fail/recover behavior.

Missing evidence remains a failed gate, not a documentation exception. The
implementation and test evidence support a **limited single-host internal
trial** only. Immutable container execution on the selected host, site PKI
lifecycle, off-site/encrypted retention, approved RPO/RTO and scheduled drills,
prior-release upgrade rehearsal, multi-host and power-loss qualification,
named owners/on-call, and deployment exclusion of standalone admission-bypassing
CLI entrypoints remain blockers to an unqualified operational claim.
