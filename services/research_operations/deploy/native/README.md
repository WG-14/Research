# Official native deployment

This is the sole supported deployment profile. One qualified Linux host runs
PostgreSQL 16, Nginx 1.24 or newer, and the systemd units in `systemd/`.
`../compose.yaml` is a non-official portability reference and is not release
evidence.

The profile intentionally fails before migration or traffic if release
metadata, named operational owners, storage qualification, production PKI,
secret permissions, encrypted off-site export, retention, RPO, or RTO policy is
missing. The repository contains no production secret, private key, htpasswd,
database, report, artifact, backup, or restore namespace.

## Host and release layout

Create the fixed service identity and external roots before installation:

```text
research-ops:research-ops                 fixed, non-login worker identity
research-web (Group=research-ops)         distinct, non-login Web identity
/opt/research-platform/releases/<git-sha> root-owned immutable clean checkout
/opt/research-platform/current            root-owned symlink to one release
/etc/research-ops/runtime.env              0640 root:research-ops
/etc/research-ops/secrets/*                0600 service or 0640 root:service
/etc/research-ops/secrets/operated-execution.key 0400 root:root, exactly 32 bytes
/etc/research-ops/pki/*                    organization-managed, outside Git
/srv/research/{data,artifacts,reports,cache,registry}
/srv/research-backups                      0700 research-ops:research-ops
/srv/research-offsite-receipts             0700 research-ops:research-ops
```

The release checkout and `release.json` must not be writable by the service.
Preflight validates the canonical schema for all three component versions,
web/Operations migrations, six wheel/sdist records, lock and native deployment
digests, artifact build digest, and release bundle digest. It recomputes every
aggregate digest and binds release ID, Git SHA, migration, lock, deployment,
build, and bundle digests to the environment.

Create the capability source once without a trailing newline, then keep it
root-only; the service reads only systemd's per-unit credential copy:

```sh
sudo install -d -o root -g root -m 0700 /etc/research-ops/secrets
openssl rand 32 | sudo install -o root -g root -m 0400 /dev/stdin \
  /etc/research-ops/secrets/operated-execution.key
```

Build and generate `release.json` from the clean release commit before adding
the venv or manifest to the immutable release directory. Install with the
manifest and artifact directory at absolute staging paths:

```sh
release=/opt/research-platform/releases/<git-sha>
scripts/platform install-release \
  --manifest /absolute/release-staging/release.json \
  --artifacts-dir "$release/dist/platform" \
  --venv "$release/.venv"
sudo install -o root -g root -m 0644 \
  /absolute/release-staging/release.json "$release/release.json"
```

The installer refuses a dirty/mismatched checkout or an existing venv,
revalidates all six archives, syncs only third-party locked dependencies, and
installs the three exact manifest-bound wheels with `--no-deps`. It then runs
the installed-release verifier, which rejects editable, source-directory,
sdist-derived, mixed-commit, or modified package payloads. Keep the root-owned
`dist/platform` wheel files with the immutable release: the installation's
PEP 610 records point to those files so later verification can recompute their
manifest hashes. `uv sync`,
`scripts/platform bootstrap`, `pip install -e`, and direct source-directory
installation are forbidden for the official native runtime. Make `current`
visible only after this check and the root-owned release copy are complete.
Never run a service from a developer checkout or editable installation.

Run the filesystem qualifier for all five roles and install its path-redacted
receipt at `/etc/research-ops/filesystem-qualification.json`. The native unit
sandbox treats datasets as read-only, allows only the manifest subtree beneath
them to be written, and grants the minimum artifact/report/cache/registry roots
to each process.

## Production PKI gate

Obtain separate server certificates for the employee DNS name and PostgreSQL
DNS name, the relevant issuing CA files, and the operations-client CA from the
site PKI owner. `generate-test-pki.sh` output is acceptance-only: the presence
of a `TEST_ONLY` marker rejects a production start.

Required private-key permissions are:

- proxy key: `0600 root:root`;
- PostgreSQL key: `0600 postgres:postgres`, or `0640 root:postgres`;
- application secrets and backup signing key: `0600 research-ops:research-ops`
  or `0640 root:research-ops`;
- operated-execution capability key: exactly 32 random bytes, `0400 root:root`;
- certificates and public verification keys: no group/other write permission.

Preflight verifies file type, no symlink, owner/readability, certificate age,
chain, DNS/IP identity, and public-key match without logging certificate or key
contents. Production defaults require at least 30 days of remaining validity.

## PostgreSQL 16 bootstrap and TLS verification

The native database is local to the qualified host, but application processes
always use the certificate DNS name with `verify-full`. Configure that DNS name
to resolve to loopback on the host; the shipped drop-in listens only on
`127.0.0.1` and `::1`, and the complete HBA permits only the five fixed Research
roles over TLS/SCRAM. It rejects non-TLS and all undeclared identities.

After installing the organization-issued database certificate/key/CA and the
five password files, load the reviewed runtime environment and run the
idempotent bootstrap as root:

```sh
set -a
. /etc/research-ops/runtime.env
set +a
sudo --preserve-env \
  /opt/research-platform/current/services/research_operations/deploy/native/bin/bootstrap-postgresql.sh
```

The script installs the exact release-bound drop-in at
`/etc/postgresql/16/main/conf.d/90-research-operations.conf`, installs the
complete HBA at `/etc/research-ops/postgresql/pg_hba.conf`, restarts PostgreSQL,
creates or rotates the fixed unprivileged roles, creates/owns the `research`
database idempotently, revokes public database/schema creation, and proves an
actual `verify-full` TLS session. Password values are passed through the
bootstrap process environment, never argv or logs. A failure produces no PASS
record and blocks preflight; preflight also byte-compares both installed policy
files with the immutable release. Re-run after credential rotation. Never add a
broader HBA rule before the shipped reject rules.

Install the Nginx systemd drop-in (it grants the proxy only the supplemental
`research-ops` group required for the two mode-0660 Unix sockets), render the
Nginx template to the exact configured path, validate, then reload:

```sh
sudo install -D -o root -g root -m 0644 \
  /opt/research-platform/current/services/research_operations/deploy/native/nginx/nginx.service.d/research-operations.conf \
  /etc/systemd/system/nginx.service.d/research-operations.conf
sudo /usr/bin/python3 \
  /opt/research-platform/current/services/research_operations/deploy/native/bin/render-nginx.py \
  --template /opt/research-platform/current/services/research_operations/deploy/native/nginx/research-operations.conf.template \
  --output /etc/nginx/conf.d/research-operations.conf \
  --server-name research.internal.corp
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl restart nginx
```

For renewal, validate a staged complete chain and matching key first, retain the
old files, atomically replace all active files, run preflight and `nginx -t`,
then use `systemctl reload nginx` and the PostgreSQL cluster reload. Confirm a
new TLS session and health probes before removing the previous material. For
revocation, issue a replacement first, update the client/server trust bundle,
reload both processes, verify rejection of the revoked identity, and record the
incident. Never restart both ingress and database merely to rotate a key.

## Required policy and off-site contract

Copy `runtime.env.example` to `/etc/research-ops/runtime.env` and replace every
placeholder. Do not assign invented people. Seven organization directory
identities are mandatory: service owner, security owner, data owner, on-call,
incident commander, backup owner, and recovery approver. Backup owner and
recovery approver must differ; service and security owner must differ.

Production requires a root-owned, non-writable executable at
`RESEARCH_OPS_OFFSITE_EXPORT_HOOK`. The backup service invokes it as:

```text
HOOK export --backup-directory ABS --target-id ID --encryption METHOD \
  --encryption-key-id KEY_ID --receipt ABS_NEW_RECEIPT
```

The hook must encrypt before external transfer, verify the remote object, and
atomically create a mode-0600 JSON receipt owned by `research-ops`. The receipt
has exactly these fields:

```text
schema_version=1, status=VERIFIED, backup_id, target_id, encrypted=true,
encryption, encryption_key_id, manifest_hash, remote_object_digest,
remote_object_version, uploaded_at, receipt_signature
```

`receipt_signature` is `base64:` followed by a strict Base64 encoding of an
RSA/SHA-256 or Ed25519 signature. The signed bytes are the ASCII JSON object
with `receipt_signature` removed, keys sorted, no insignificant whitespace,
and one trailing newline. Install the matching root-owned, non-writable trusted
public key at `RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE`; the export
hook's private key remains outside this service and repository. The wrapper verifies that
signature and binds the receipt to the local signed manifest before success. A
hook, key, signature, or receipt failure fails the systemd backup unit and must
alert; it never claims off-site success.

Manifest and recovery-receipt publishers create and verify payload/signature
pairs under unique temporary names before publishing final names. A signing
failure leaves no final document. A process loss between the two no-replace
publishes may leave an unsigned final payload; retry resumes only when every
non-temporal canonical field matches, signs that exact payload, and otherwise
fails closed. Never delete or replace a partial pair to force progress.

Retention automation is deliberately dry-run only. A set is complete only
after re-verifying the trusted manifest signature, every manifest-bound size
and SHA-256, the verification marker, and the policy-bound off-site receipt
signature. Age is taken from the signed manifest, not mutable file mtime. It
reports old, incomplete, and `LEGAL_HOLD` protected backup IDs to journald.
Deletion requires a separately reviewed operator action and must never remove
the configured minimum cryptographically complete copies.

## Install and start

Install every file in `systemd/` under `/etc/systemd/system/`, then run:

```sh
sudo systemctl daemon-reload
sudo systemctl start research-operations-preflight.service
sudo systemctl start research-operations-migrate.service
sudo systemctl enable --now research-operations.target
sudo systemctl enable --now research-operations-backup.timer
sudo systemctl enable --now research-operations-preflight.timer
sudo systemctl enable --now research-operations-retention-audit.timer
```

The target starts two outbox instances, one admitted job worker, persistent
validator, web service, diagnostics API, and Nginx. Every long-running
operational unit runs as `research-ops`; the employee Web unit runs as the
distinct `research-web` UID with `Group=research-ops`. Only the admitted job
worker receives `operated-execution.key` through systemd `LoadCredential`.
The source key remains root-only and Web cannot mint an execution capability.
All units restart on failure, send SIGTERM,
allow a bounded drain interval, use a private Linux `/tmp`, write only to
declared roots, drops all capabilities, applies task/memory/CPU/file limits,
and logs to journald. Nginx reaches both Gunicorn processes only through
permission-limited Unix sockets; there are no application TCP listener ports.
Employee TLS is 443 and the mTLS operations listener is loopback 9443.

Inspect without exposing secrets:

```sh
systemctl status research-operations.target
systemctl list-units 'research-operations-*'
journalctl -u 'research-operations-*' --since today
curl --fail --cacert SITE_CA https://research.internal.corp/__not-an-ops-path
curl --fail --cert OPS_CLIENT_CERT --key OPS_CLIENT_KEY \
  https://127.0.0.1:9443/__ops/ready/workflow-mutation
```

Daily preflight detects approaching certificate expiry and policy/release
drift. It atomically refreshes a root-owned, group-readable canonical
observation in `/run/research-operations-preflight/observation.json`; a failed
run replaces PASS with a secret-free FAIL code. Workflow readiness rejects a
missing, stale, malformed, failed, permission-unsafe, or release-mismatched
observation. A failed periodic preflight is also an incident and alert source;
application readiness remains independently fail-closed on database,
migration, worker, validator, outbox, receipt, fence, and release mismatch.
After a host reboot, verify the two worker heartbeats and a fresh validator
observation before admitting mutations.

## Stop, restart, and recovery

Close mutation admission and wait for drain counts before stopping the target.
Systemd sends SIGTERM and only uses SIGKILL after each unit's documented grace
period. Never restore over the active namespace. Use the signed blank-restore
and explicit activation procedure in `../../docs/runbook.md`, re-run the owner
migration/ACL gate, requalify storage, then start this target against the newly
activated namespace.
