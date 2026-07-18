# Research Operations

`research-operations` is the operational trust domain embedded in the Market
Research Platform monorepo. It coordinates the authenticated offline research
service without adding trading, account access, order/fill ingestion, or
network market-data collection.

Runtime and operator commands are exposed through `research-ops`:

- `migrate`;
- `audit-validate`;
- `metrics`;
- `outbox-scan`, `outbox-worker`, and authorized `outbox-requeue`;
- `research-job-worker`;
- allowlisted `admitted-run` and read-only `admission-status`;
- `backup-fence begin|status|seal|reopen|quarantine`;
- `backup-manifest-create` and `backup-verify`;
- `recovery-verify` and explicit `recovery-activate`.

Lease capabilities and secret material are never accepted through arbitrary
command tails or emitted to stdout. The admitted execution adapter is
allowlisted and calls Research in-process after authorization and durable
admission; it is not a shell-command surface.

## Monorepo boundaries

Operations depends on:

- `market_research.application` and its explicit adapter contracts;
- `market_research_web.operations_contract` for the supported web/portal
  surface.

It must not import portal or Research implementation modules directly.
Research never depends on Operations. All three distributions use the root
workspace lock and one release manifest.

## Schema migrations

- `0001_initial.sql`: durable outbox delivery, worker heartbeat, experiment
  identity/request, and active admission claim;
- `0002_runtime_control.sql`: mutation/claim fence, validation observation,
  signed backup registration, and restore-drill evidence;
- `0003_research_job_receipt.sql`: fenced result publication receipt bridging
  admission completion and the Django terminal update;
- `0004_worker_release_provenance.sql`: release SHA/ID/build provenance for
  worker heartbeats and backup sets.
- `0005_recovery_activation_event.sql`: append-only, content-hashed recovery
  activation evidence bound to the backup, signed receipt, release bundle,
  operator, and runtime-control transition.
- `0006_service_alert_workflow.sql`: allowlisted offline service-health alerts,
  fenced delivery claims, actor-separated acknowledgement, deadline escalation,
  and append-only hash-chained event evidence.

The owner-only migration gate applies Django and Operations migrations,
collects static assets, revokes inherited privileges, and grants the runtime,
diagnostics, validator, and backup roles only their declared capabilities.
Because coherent dumps omit privilege ownership, the gate runs again against an
activated restore before runtime processes start.

## Coordination contracts

`OutboxStore` scans immutable web audit intents into durable delivery state.
Claims use `FOR UPDATE SKIP LOCKED`; terminal updates compare worker identity,
opaque lease token, increasing fencing token, expiry, and payload hash.
Transient failures use bounded retry; permanent/exhausted failures enter a
bound dead-letter state that requires an authorized operator decision.

`ServiceAlertStore` is a separate operational-health workflow. Its condition
allowlist covers database, worker, audit, backup, restore, certificate,
preflight, and readiness failures only. Exact raises converge on an immutable
request binding; receiver delivery uses a fenced lease and stable idempotency
key; acknowledgement records actor, UTC time, and a bounded reason code and
must be performed by an actor other than the source probe. Unacknowledged due
alerts create a new escalation-level delivery. Every state transition appends
a hash-chained event protected by a PostgreSQL update/delete rejection trigger.
The HTTP adapter permits HTTPS or loopback HTTP, never follows redirects, and
sends no paths, credentials, free-text exception bodies, or topology labels.

Receiver URLs are read only from the owner-controlled private file named by
`RESEARCH_OPS_ALERT_ENDPOINT_URL_FILE`; they are not accepted in argv, stored
in PostgreSQL, emitted in command output, or included in alert evidence.

`ExperimentAdmissionStore` serializes `(authority, experiment_id)` across web
and admitted CLI adapters. Exact requests converge, different active requests
conflict, expired claims receive a higher fence, and stale tokens cannot
publish.

The persistent research worker holds both job and experiment leases. Admission
completion and the Operations result receipt commit atomically. Applying that
receipt to the Django terminal job is a second explicit transaction; a crash in
the window is reconciled from the receipt without rerunning the engine. An
unapplied receipt blocks readiness and backup sealing.

During backup `DRAINING`, mutation admission closes while committed audit
intents finish. During `SEALED`, both mutation and claim admission close. Every
claim transaction orders against the singleton fence row.

## Release and runtime profile

The canonical root-generated `release.json` binds the Git SHA, all three
packages and artifacts, lock, migrations, native deployment assets, and
aggregate digests. Workers publish the configured release identity in durable
heartbeats; readiness fails closed on a missing or mixed release.

The official service sets `RESEARCH_RUNTIME_PROFILE=operated`, which disables
the direct `market-research` entrypoint. Service-host execution must enter
through Operations admission and fencing. Each invocation consumes a
process/thread-bound one-shot capability HMAC-bound to the active PostgreSQL
claim ID, lease token, fence, request hash, expiry, and exact execution scope.
Only the job-worker unit receives the 32-byte root-owned source through
systemd `LoadCredential`; the Web unit runs under a distinct UID and cannot
mint a capability. A production `admitted-run` must likewise be launched by a
root-reviewed credential-bearing systemd unit or transient unit; running the
CLI directly under the service account fails closed.

## Deployment and evidence

The sole official deployment is `deploy/native`: PostgreSQL 16, Nginx,
Gunicorn, and systemd on one qualified Linux host. `deploy/compose.yaml` is a
non-official portability reference and is not acceptance evidence.

See:

- `deploy/native/README.md` for installation and preflight;
- `docs/runbook.md` for observation, backup, recovery, upgrade, and rollback;
- the monorepo `docs/release-checklist.md` for promotion evidence.

The Operations PostgreSQL CI job performs a repository-level E4 blank-restore
rehearsal for every change. It creates representative immutable dataset,
manifest, completed-job, audit, report, and reproduction evidence; runs the
official signed backup flow; restores with `pg_restore` into a newly created
blank database and filesystem namespace; and verifies release, migration,
audit, object-reference, dataset, report, and reproduction bindings before
deleting only that random target database. The signed recovery receipt records
the measured duration. This CI evidence is not E5 production proof and does not
establish organization PKI, encrypted off-site custody, named-owner separation
of duties, or an approved/promoted RPO or RTO.

The repository supplies contracts and tests, not organization approval. Every
promoted release still needs real named owners, organization PKI and alerting,
external secret rotation, encrypted off-site storage, approved retention/legal
hold and RPO/RTO, target-host qualification, and release-specific PostgreSQL,
browser, TLS, restart, upgrade, backup, and blank-restore evidence.
