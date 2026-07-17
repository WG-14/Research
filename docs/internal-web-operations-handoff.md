# Internal Web Operations Handoff

## Purpose

The operational trust domain for the internal web service is
`services/research_operations` in this monorepo. This handoff identifies what
the code owns, what a site operator must supply, and which evidence is required
before promoting a release.

The platform is an offline research service. Operations must not add exchange
access, accounts, orders, fills, live market-data collection, or runtime
trading controls.

## Ownership boundary

| Area | Code owner | Runtime authority |
| --- | --- | --- |
| Research semantics and artifacts | `market-research` | Immutable external datasets, artifacts, reports, and registries |
| Identity, permissions, web jobs, managed report catalog | internal web adapter | PostgreSQL plus hash-verified managed objects |
| Job/outbox leases, fencing, admission, heartbeats, backup fence | Operations service | PostgreSQL `research_ops` schema |
| Process supervision and restart | native deployment | systemd |
| Employee and operations ingress | native deployment | Nginx and organization PKI |
| Backup and recovery | Operations service and runbook | signed local set, encrypted off-site copy, explicit activation receipt |
| Owners, alerts, RPO/RTO, PKI lifecycle | organization | approved directory identities and external systems |

No person may be invented to satisfy a preflight field. The service owner and
security owner must differ, and the backup owner and recovery approver must
differ. The official example requires stable identities for service, security,
data, on-call, incident command, backup, and recovery approval.

## Installed release

Promote only an immutable, root-owned clean checkout at a path such as:

```text
/opt/research-platform/releases/<git-sha>
/opt/research-platform/current -> releases/<git-sha>
```

Install the three distributions together from the root frozen workspace. The
root-generated `release.json` must bind the same Git SHA, release ID, build
digest, package artifacts, lock, migrations, and native deployment files. It
must not be writable by `research-ops`.

Set the exact manifest values in:

- `RESEARCH_OPS_GIT_SHA`;
- `RESEARCH_OPS_RELEASE_ID`;
- `RESEARCH_OPS_BUILD_DIGEST`;
- `RESEARCH_OPS_EXPECTED_MIGRATION_DIGEST`.

All web, API, outbox, job-worker, validator, and backup processes use that
identity. Readiness rejects missing or mixed worker release provenance. A
rolling mixed release is therefore not supported unless a future reviewed
protocol explicitly permits it.

The official runtime also sets `RESEARCH_RUNTIME_PROFILE=operated`. Direct
`market-research` invocation is blocked on the service host; research work
enters through Operations admission, identity binding, leases, and fencing.

## External filesystem layout

The repository contains no runtime state or private material. Provision and
qualify, at minimum:

```text
/etc/research-ops/runtime.env
/etc/research-ops/secrets/
/etc/research-ops/pki/
/srv/research/data/
/srv/research/artifacts/
/srv/research/reports/
/srv/research/cache/
/srv/research/registry/
/srv/research-backups/
/srv/research-offsite-receipts/
```

The service account is fixed and non-login. The release tree is read-only to
it. Dataset paths are read-only except for the explicitly qualified registry
subtree. Artifact, report, cache, registry, backup, and receipt roots have
separate declared roles. Symlinks, repository-contained paths, unsafe modes,
and unqualified filesystems fail preflight.

Credentials, database URLs, Django secret material, backup signing keys,
htpasswd, certificates, and private keys are injected from root-owned external
files. They must never be placed in `runtime.env`, the source tree, a release
manifest, command arguments, health responses, or audit events.

## Database and migrations

PostgreSQL 16 is the supported operating database. Keep owner, runtime,
diagnostics, validator, and backup roles separate. TLS identity verification is
required for application connections.

The owner-only migration gate applies both migration families from the same
release:

- Django migrations under `apps/internal_web/src/portal/migrations`;
- Operations migrations under
  `services/research_operations/src/research_operations/migrations`.

It then collects static assets and reapplies least-privilege grants. Dumps omit
privilege ownership deliberately, so the migration/ACL gate must run again
against an activated restore before runtime roles start.

Never run a schema downgrade over the active production database as an
improvised rollback. Rehearse forward migration and application rollback with
the previous and candidate release in isolated namespaces, and retain the
result with the release record.

## Supervised processes

The official deployment is
`services/research_operations/deploy/native`. The target contains:

- Django web Gunicorn service;
- mTLS-protected Operations diagnostics API;
- two independently identified outbox workers;
- one persistent admitted research job worker;
- a persistent audit validator;
- preflight and scheduled backup/retention timers.

Systemd applies a non-login service user, private temporary directory,
read/write allowlists, dropped capabilities, bounded tasks/memory/CPU/files,
restart policy, SIGTERM, and a finite drain timeout. Journald is the local log
authority; log forwarding and alert routing are site responsibilities.

Nginx is the sole employee TLS ingress. The Operations listener is separate,
loopback-bound, and requires a client certificate. Proxy headers are trusted
only from the declared local proxy boundary.

`services/research_operations/deploy/compose.yaml` is a non-official reference.
It is not an alternate supported production path and cannot be used as release
acceptance evidence.

## Readiness and health

Liveness answers only whether a process can respond. It must not conceal
dependency failure. Workflow readiness is stricter and remains closed when any
required observation fails or becomes stale, including:

- database connection, expected schema, or migration digest;
- configured release or worker release mismatch;
- missing/stale outbox or job worker heartbeat;
- audit validator failure or stale observation;
- outbox lag, dead-letter, orphan, duplicate, or integrity failure;
- unapplied research-job result receipt;
- active backup fence or mutation admission closure;
- backup/restore evidence outside the configured age policy.

Diagnostics must remain bounded and path/secret redacted. Monitoring should
alert on the reason code and release ID, not scrape or export raw database rows.

After boot or restart, do not admit mutations until preflight passes, migrations
are confirmed, the expected worker counts report the same release, and a fresh
validator observation is visible.

## Audit and job recovery

Web state mutation and the immutable audit intent commit together in
PostgreSQL. Outbox workers project each intent to the external append-only
stream using idempotent event identity. Claims use bounded leases and fencing;
a stale worker cannot publish after another worker acquires a higher fence.
Permanent/exhausted failures enter the dead-letter state and require an
authorized, auditable requeue decision.

The admitted research worker holds both the Django job lease and the
experiment admission lease. It validates immutable manifest and result
bindings before publication. Admission completion and the Operations result
receipt commit together; applying the receipt to the Django terminal job is a
second explicit transaction. An unapplied receipt blocks readiness and backup
sealing until reconciled without rerunning the research engine.

Expired leases and orphan artifacts are evidence for investigation. They do
not authorize a stale worker or an ad hoc shell command to mutate state.

## Backup and recovery

Follow `services/research_operations/docs/runbook.md`; do not restore over an
active namespace.

The high-level sequence is:

1. close mutation admission and drain active work;
2. seal the backup fence after outbox and receipt checks pass;
3. capture the coherent PostgreSQL dump and declared external evidence roots;
4. create and verify signed backup metadata bound to the release;
5. invoke the externally installed encryption/off-site hook;
6. verify the new immutable off-site receipt;
7. reopen admission only after local and off-site completion is recorded.

Recovery uses a new empty database and empty filesystem roots. Verify the
signature, release/migration binding, file hashes, registry/audit integrity,
and no-follow path rules before running migrations/ACLs. Exercise readiness and
representative authenticated read/audit workflows in isolation. Activation is
an explicit approved step with a receipt; it is never implied by verification.

Retention automation is dry-run only. It identifies eligible, incomplete, and
legal-hold sets but does not delete evidence. Any deletion workflow requires a
separate reviewed operator action and must preserve the configured minimum
complete copies.

## PKI and secret lifecycle

The site PKI owner must issue separate employee-server, PostgreSQL-server, and
operations-client trust material. Test certificates are rejected for
production. Preflight checks regular-file/no-symlink rules, ownership, modes,
chain, identity, public-key match, and minimum remaining lifetime without
printing key contents.

Renewal must stage and validate a complete chain and matching key, atomically
replace active files, rerun preflight and proxy/database validation, reload,
and establish a new TLS session before retiring old material. Revocation must
replace trust material, verify rejection of the revoked identity, and create an
incident record. The repository does not supply a corporate CA, certificate
inventory, notification target, or renewal authority.

## Promotion blockers owned outside the repository

A source tree cannot close these gates. Promotion remains blocked until the
site supplies evidence for all of them:

- named owner and on-call assignments with escalation targets;
- organization PKI issuance, renewal, revocation, and expiry alerts;
- production secret distribution and rotation;
- an executable, root-owned encrypted off-site export implementation and
  independently controlled destination;
- retention and legal-hold approval;
- approved RPO/RTO plus measured backup and blank-restore drill results;
- alert integration for systemd, preflight, readiness, certificate, outbox,
  backup, and restore failures;
- target-host and storage qualification, including reboot and power-loss scope;
- release-specific PostgreSQL, browser, TLS, restart/drain, upgrade, rollback,
  backup, and recovery acceptance with zero unexpected skips.

Missing external evidence is a failed gate, not a documentation exception.
