# Research Operations deployment and recovery runbook

This runbook covers the official native systemd deployment of the offline
Research web service. It is not a trading runtime. PostgreSQL 16 and Nginx are
host services; systemd supervises the platform applications and workers.

`deploy/compose.yaml` is a non-official portability reference. Do not use a
Compose render, container image, or a prior development acceptance record as
evidence for the official deployment.

## 1. Release inputs

Build from one clean monorepo commit and generate the canonical release
manifest outside the source tree:

```sh
scripts/platform bootstrap
scripts/platform build
scripts/platform release-manifest \
  --release-id platform-YYYY.MM.DD.N \
  --artifacts-dir "$PWD/dist/platform" \
  --output /absolute/release-staging/release.json
```

The manifest binds the Git SHA, all three component versions and six built
artifacts, root lock, Django and Operations migrations, native deployment
files, build digest, and release-bundle digest. Promote the clean checkout to
`/opt/research-platform/releases/<git-sha>`, install the frozen workspace, copy
the reviewed `release.json` into that immutable release, and expose it through
the root-owned `current` symlink. Neither checkout nor manifest may be writable
by `research-ops`.

Copy `deploy/native/runtime.env.example` to
`/etc/research-ops/runtime.env`, replace every placeholder, and set mode `0640`
with `root:research-ops`. Bind all release values exactly, including:

- `RESEARCH_OPS_GIT_SHA`;
- `RESEARCH_OPS_RELEASE_ID`;
- `RESEARCH_OPS_BUILD_DIGEST`;
- `RESEARCH_OPS_EXPECTED_MIGRATION_DIGEST`;
- `RESEARCH_OPS_LOCK_DIGEST`;
- `RESEARCH_OPS_DEPLOYMENT_DIGEST`;
- `RESEARCH_OPS_RELEASE_BUNDLE_DIGEST`.

The service profile must set `RESEARCH_RUNTIME_PROFILE=operated`. Verify that
direct `market-research` invocation exits fail-closed. Only the Operations
admission worker may use the explicit admitted Research adapter.

Do not put secret values in the environment file. Passwords, database URLs,
Django secret, htpasswd, signing keys, certificates, and private keys are
external files with the documented identities and modes.

## 2. Organization and host prerequisites

Do not invent assignments to pass preflight. Obtain real directory identities
for service, security, data, on-call, incident command, backup, and recovery
approval. Service/security and backup/recovery-approval identities must differ.

Obtain organization-issued employee-server, PostgreSQL-server, and operations-
client PKI. Test PKI is rejected for production. Establish certificate renewal,
revocation, and expiry alerts before promotion.

Provision external roots for datasets, artifacts, reports, cache, registries,
backups, off-site receipts, and recovery namespaces. Qualify the active roots:

```sh
/opt/research-platform/current/.venv/bin/python \
  services/research_operations/scripts/qualify-filesystem.py \
  --root data=/srv/research/data \
  --root artifact=/srv/research/artifacts \
  --root report=/srv/research/reports \
  --root cache=/srv/research/cache \
  --root identity_registry=/srv/research/registry/research_validate_experiment_identity.jsonl \
  --output /etc/research-ops/filesystem-qualification.json
```

A single-host receipt is not multi-host qualification. Keep
`RESEARCH_OPS_DEPLOYMENT_SCOPE=single-host` unless cross-host locking,
atomic-publication, append, crash, and power-loss behavior has independent
evidence.

Install a root-owned, non-writable executable at
`RESEARCH_OPS_OFFSITE_EXPORT_HOOK`. It must encrypt before transfer, verify the
remote object, and atomically publish the exact receipt contract described in
`deploy/native/README.md`. Set approved retention, legal-hold, RPO, and RTO
values; example values are not approvals.

## 3. Preflight and installation

Install the files from `deploy/native/systemd` in `/etc/systemd/system`, render
the Nginx template to the declared root-owned path, and validate both systems:

```sh
sudo systemd-analyze verify /etc/systemd/system/research-operations*.service \
  /etc/systemd/system/research-operations*.target \
  /etc/systemd/system/research-operations*.timer
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl start research-operations-preflight.service
```

Preflight validates the complete release manifest and recomputed aggregate
digests, operated profile, service identities, owner assignments, source and
runtime file modes, external root qualification, secret files, PKI identity and
lifetime, native tools, off-site hook, retention, legal hold, RPO, and RTO.

It atomically writes
`/run/research-operations-preflight/observation.json` as mode `0640`
`root:research-ops`. The receipt contains only status, timestamp, release
identity, bundle digest, and a stable failure code. A FAIL, stale, mismatched,
or missing receipt blocks runtime entrypoints. Journal output and the receipt
must remain secret-free.

Apply the owner-only migration and ACL gate, then enable the target and timers:

```sh
sudo systemctl start research-operations-migrate.service
sudo systemctl enable --now research-operations.target
sudo systemctl enable --now research-operations-backup.timer
sudo systemctl enable --now research-operations-preflight.timer
sudo systemctl enable --now research-operations-retention-audit.timer
```

The migration service applies Django migrations, Operations SQL migrations,
collects static assets, revokes inherited grants, and reapplies the declared
runtime, diagnostics, validator, and backup capabilities.

## 4. Probes and normal observation

The target starts web, operations API, two outbox workers, one admitted research
job worker, and one persistent audit validator. Confirm that all worker
heartbeats carry the configured Git SHA, release ID, and build digest.

```sh
systemctl status research-operations.target
systemctl list-units 'research-operations-*'
journalctl -u 'research-operations-*' --since today
curl --fail --cacert SITE_CA https://research.internal.corp/
curl --fail --cert OPS_CLIENT_CERT --key OPS_CLIENT_KEY \
  https://127.0.0.1:9443/__ops/ready/workflow-mutation
```

The employee listener must reject operations paths. The loopback operations
listener requires a valid client certificate and the configured authorization
for diagnostics and metrics.

Readiness closes on database or migration failure, stale/missing workers,
release mismatch, stale validator observation, audit integrity or lag, DLQ,
unapplied result receipt, backup fence, or quarantine. Liveness is deliberately
weaker and must not be used to admit mutable workflows.

Alert from stable reason codes and systemd state. Do not export paths, URLs,
usernames, request bodies, cookies, DSNs, environment values, or payload labels.
Daily preflight failure and approaching certificate expiry are incidents.

The built-in service-health alert workflow is restricted to the allowlist in
`research_operations.alerting`; it is not a market or trading monitor. Configure
each supervised delivery unit with a mode-0600
`RESEARCH_OPS_ALERT_ENDPOINT_URL_FILE`, then exercise the release-specific
receiver before promotion. Raise with a stable incident idempotency key, run
`alert-deliver-once` for the bound endpoint, record acknowledgement with a
different actor and a stable reason code, and run `alert-escalate-once` from a
timer. An acknowledged incident must not escalate. Retain the alert ID and
terminal event hash with the receiver's independently retained idempotency key.
Test both the acknowledgement path and an intentionally unacknowledged deadline
that reaches the secondary receiver. A repository loopback receiver proves the
transport and PostgreSQL workflow but does not establish the organization's
actual on-call ownership or external receiver availability.

## 5. Stop, restart, and host reboot

Before planned shutdown, close mutation admission and wait for active mutation,
job, admission, outbox, and unapplied receipt counts to drain. Then stop the
target. Systemd sends SIGTERM; workers stop claiming new work and complete only
the lease-bound operation within the unit timeout.

```sh
sudo systemctl stop research-operations.target
```

Do not generically requeue an expired research job. Inspect its immutable
result receipt and current fencing state. A stale executor must never publish
after another executor owns a higher fence.

After restart or reboot, run preflight and verify migrations, expected worker
counts/releases, and a fresh validator observation before reopening mutable
work. Include crash restart, bounded drain, database failure/recovery, and reboot
results in the release acceptance record.

## 6. Coherent backup and off-site export

The native backup timer runs `deploy/native/bin/native-backup.sh`, which wraps
the two-phase fenced backup and mandatory off-site export:

1. close mutation admission while existing audit claims drain;
2. wait for active jobs, experiment claims, outbox work, audit intents, and
   unapplied result receipts to reach zero;
3. record a fresh validator observation, then seal all claim admission;
4. capture the PostgreSQL dump and declared external evidence roots;
5. create and detached-sign a canonical manifest bound to release, build,
   migrations, fence, audit terminal state, sizes, and hashes;
6. independently verify and register that exact manifest;
7. invoke encrypted off-site export and verify its bound immutable receipt;
8. emit a non-destructive retention/legal-hold inventory.

Both manifest and recovery receipt signing happen against unique temporary
payload/signature files and are verified before final-name publication. A
signing failure therefore leaves no final-name orphan. If publication is
interrupted after the canonical payload becomes durable, preserve it and retry
the same backup/recovery operation: the publisher resumes only the exact
non-temporal state and fails closed on any conflict.

Run an unscheduled backup through systemd so it uses the same identity,
sandbox, secrets, and off-site contract:

```sh
sudo systemctl start research-operations-backup.service
systemctl status research-operations-backup.service
journalctl -u research-operations-backup.service --since today
```

Any failure leaves evidence for investigation and may leave admission closed.
Never delete the private fence receipt or automatically force the fence open.
Resume only the exact backup ID using the documented `BACKUP_RESUME_ID`
contract after diagnosing the failure. Fence intent is created durably at
`/run/research-operations/backup-fence-<backup-id>.json` before the database
commit. Resume first reconciles that owner-only, mode-0600 intent with the exact
database fence token/generation; a missing, symlinked, misowned, permissive, or
mismatched receipt fails closed. The runtime directory itself must be the
owner-only mode-0700 systemd directory and is never under `/tmp`. A local signed
set without a verified encrypted off-site receipt is not a successful
production backup.

The off-site hook signs the canonical receipt-without-`receipt_signature` using
the separately controlled RSA/SHA-256 or Ed25519 key and encodes the signature
as `base64:<strict-base64>`. The installed trusted public key is configured by
`RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE`. Retention automation is
dry-run only and counts a copy as complete only after re-verifying the backup
manifest signature, every bound size/hash, the verification marker, and that
policy-bound off-site signature. Deletion requires a separate authorized,
reviewed action that honors legal hold and preserves the configured minimum
cryptographically complete copies.

## 7. Isolated signed restore rehearsal

Never restore over an active namespace. Provision a new empty PostgreSQL
database and empty filesystem namespace, with separate recovery credentials.
Point the recovery environment at those targets and run:

```sh
services/research_operations/scripts/restore-rehearsal.sh \
  /srv/research-backups/<backup-id> \
  /srv/recovery/<new-id> \
  /srv/recovery-receipts/<new-id>.json
```

The script verifies signature and checksums before mutation, rejects nonempty
targets and unsafe archive members, restores without `--clean`, and marks the
namespace isolated/read-only. Verification binds the exact release/build and
migrations; database/filesystem hashes; closed admission and zero-writer state;
job results and decision reports; segmented audit/outbox terminal watermark;
governance and identity registries; and approval artifacts. Missing, skipped,
pending, orphaned, drifted, or mismatched evidence fails the signed receipt.

If interrupted after database restore but before receipt publication, preserve
the target and rerun the same command only with the documented
`RESEARCH_OPS_RECOVERY_RESUME=true`. Resume accepts the exact database,
namespace, manifest, and partial receipt state; it is not a repair mode.

The Operations PostgreSQL CI job exercises this same pair of backup/restore
scripts, rather than a mocked archive reader. It builds the clean-checkout
release artifacts and canonical release manifest, creates representative
immutable research/Web/Operations evidence, restores into a random new blank
database and filesystem namespace, revalidates target-resolved object and
reproduction bindings, verifies the signed receipt and recorded duration, and
then drops only the guarded random target. The Operations JUnit gate rejects
all skipped tests, so this rehearsal cannot silently skip in CI. Treat that as
repository-level E4 regression evidence only: production promotion still
requires an independently retained receipt from the qualified target host and
does not inherit E5, organization-PKI, off-site-custody, named-owner, or
approved RPO/RTO status from CI.

After independent review, activation is separate and explicit:

```sh
research-ops recovery-activate \
  --backup-directory /srv/research-backups/<backup-id> \
  --restore-namespace /srv/recovery/<new-id> \
  --receipt-path /srv/recovery-receipts/<new-id>.json \
  --postgresql-major 16 \
  --operator-id reviewed-recovery
```

Activation re-verifies the signed receipt and manifest, records the drill
idempotently, checks the restored fence and zero-writer state under the
exclusive lock, and opens admission. An exact retry converges. A crash after
removing database read-only still leaves runtime admission sealed.

Run the owner migration/ACL gate against the activated database because dumps
contain neither role definitions nor ACLs. Requalify the restored filesystem,
start both outbox workers, job worker, validator, API, and web processes for the
same release, then wait for readiness before traffic.

## 8. Upgrade rehearsal

Every candidate must be rehearsed before production:

1. create and verify a signed, off-site current-release backup;
2. restore it into a blank isolated database and filesystem namespace;
3. install the candidate clean checkout and candidate `release.json` in a
   separate immutable release directory;
4. run candidate preflight against staging paths and real-form policy inputs;
5. apply candidate migrations/ACLs to the isolated restored database;
6. start candidate web/API/workers with the candidate release identity;
7. execute authenticated reads, admitted validation, worker competition,
   outbox exactly-once behavior, audit validation, backup, and recovery probes;
8. verify candidate backup/restore evidence and the declared RTO;
9. test the chosen rollback branch below; record all versions and digests.

Do not call a fresh install an upgrade rehearsal. The input must contain the
selected prior release's real schema/evidence shapes.

## 9. Production upgrade and rollback

Before switching releases, close admission, drain, and take a signed verified
backup with a verified off-site receipt. Keep the prior immutable release and
its environment/manifest available. Stop the target, atomically switch the
root-owned `current` symlink and reviewed environment to the candidate, run
preflight, run the migration gate once, then start and observe the target.

Define the rollback decision owner, health thresholds, and maximum observation
window before the change.

There are two allowed rollback paths:

- **Application rollback:** switch back to the prior immutable release only if
  rehearsal proved that exact prior application can safely run against the
  candidate-migrated schema. Rerun prior preflight and verify all worker release
  identities before reopening admission.
- **Signed-restore rollback:** otherwise stop the candidate and restore the
  pre-upgrade signed backup into a new blank database/filesystem namespace.
  Use the prior release code, manifest, release environment, and verification
  keys so release binding matches the backup. Complete signed restore review,
  explicit activation, owner migration/ACL application, storage qualification,
  supervised startup, and readiness before switching traffic.

Never run an improvised destructive schema downgrade and never restore over the
candidate namespace. Preserve the failed candidate namespace and logs for
investigation. Record which rollback path ran, the approval identity, backup
and recovery receipt hashes, achieved RPO/RTO, and final release provenance.

## 10. Promotion blockers and evidence retention

No fixed historical digest, UUID, or past development test count in this
runbook is current release evidence. For each release, archive the exact clean
commit, release manifest, CI results, real PostgreSQL/browser/TLS/restart
results, preflight PASS receipt, storage qualification, system inventory,
signed backup, off-site receipt, restore/activation receipt, upgrade/rollback
rehearsal, and residual-risk approvals.

Promotion remains blocked without real owner/on-call assignments,
organization-managed PKI lifecycle and alerts, external secret rotation,
encrypted off-site storage, approved retention/legal hold and RPO/RTO, target-
host/storage qualification, and release-specific acceptance with zero
unexpected skips.
