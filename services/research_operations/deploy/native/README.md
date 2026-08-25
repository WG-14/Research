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

Create the fixed, non-login trust-tier identities and external roots before
installation:

```text
research-ops                              shared research-filesystem group only
research-web-proxy                        employee Web socket group only
research-ops-proxy                        diagnostics socket group only
research-migrate                          owner-schema credential tier
research-web                              employee Web credential tier
research-outbox                           runtime outbox credential tier
research-job                              admitted-execution credential tier
research-alert                            receiver credential tier
research-validator                        independent validator credential tier
research-backup                           backup/signing credential tier
research-diagnostics                      read-only diagnostics credential tier
research-retention                        secret-free retention audit tier
research-proxy                            dedicated Nginx worker identity
/opt/research-platform/releases/<git-sha> root-owned immutable clean checkout
/opt/research-platform/current            root-owned symlink to one release
/etc/research-ops/runtime.env              0600 root:root
/etc/research-ops/secrets/*                0600 root:root source secrets
/etc/research-ops/secrets/operated-execution.key 0400 root:root, exactly 32 bytes
/etc/research-ops/backup-signing.pub       0644 root:root public key
/etc/research-ops/dataset-transformation-trust.json 0644 root:root canonical trust store
/etc/research-ops/dataset-transformation-keys/*.ed25519.pub 0644 root:root public keys
/etc/research-ops/independent-verifier-trust.json 0644 root:root canonical trust store
/etc/research-ops/independent-verifier-keys/*.ed25519.pub 0644 root:root public keys
/etc/research-ops/pki/*                    organization-managed, outside Git
/srv/research/data                         2750 root:research-ops (immutable input)
/srv/research/data/_internal_web/manifests 2770 root:research-ops
/srv/research/{artifacts,reports,cache,registry} 2770 root:research-ops
/srv/research/artifacts/_internal_web     2770 root:research-ops
/srv/research/artifacts/_internal_web/static 0755 research-migrate:research-ops
/srv/research/artifacts/reports/research/_registry 2770 research-web:research-ops
/srv/research/artifacts/derived/research/projects 2770 research-web:research-ops
/srv/research/reports/_internal_web       2770 research-web:research-ops
/srv/research/cache/research/projects     2770 research-web:research-ops
/srv/research/artifacts/_operations_sandbox 2770 research-job:research-ops
/srv/research-backups                      0750 research-backup:research-backup
/srv/research-offsite-receipts             0750 research-backup:research-backup
```

Install the reviewed identity declaration and create the accounts before
starting preflight:

```sh
sudo install -D -o root -g root -m 0644 \
  deploy/native/sysusers.d/research-operations.conf \
  /etc/sysusers.d/research-operations.conf
sudo systemd-sysusers /etc/sysusers.d/research-operations.conf
sudo install -d -o root -g research-ops -m 2750 /srv/research/data
sudo install -d -o root -g research-ops -m 2770 \
  /srv/research/data/_internal_web/manifests \
  /srv/research/artifacts /srv/research/reports \
  /srv/research/cache /srv/research/registry \
  /srv/research/artifacts/_internal_web
sudo install -d -o research-migrate -g research-ops -m 0755 \
  /srv/research/artifacts/_internal_web/static
sudo install -d -o research-web -g research-ops -m 2770 \
  /srv/research/artifacts/reports/research/_registry \
  /srv/research/artifacts/derived/research/projects \
  /srv/research/reports/_internal_web \
  /srv/research/cache/research/projects
sudo install -d -o research-job -g research-ops -m 2770 \
  /srv/research/artifacts/_operations_sandbox
sudo install -o root -g research-ops -m 0660 /dev/null \
  /srv/research/registry/final_holdout_authority.jsonl
sudo install -o root -g research-ops -m 0660 /dev/null \
  /srv/research/registry/final_holdout_authority.jsonl.lock
sudo /usr/bin/chattr +a \
  /srv/research/registry/final_holdout_authority.jsonl
sudo install -d -o research-backup -g research-backup -m 0750 \
  /srv/research-backups /srv/research-offsite-receipts
```

Preflight rejects a missing, login-enabled, UID/GID-aliased, unexpected
primary-GID member, host-supplementary-grouped, or incorrectly grouped tier.
The only persistent controlled-group memberships are `research-proxy` in
`research-web-proxy` and `research-ops-proxy`; Nginx's worker-side
`initgroups(3)` therefore retains exactly the two socket permissions. Do not
map two names to one numeric identity or add service accounts to host groups.
The backup service uses mode `0750` backup directories and mode `0640` files;
only the read-only, AF_UNIX-only retention auditor joins the `research-backup`
group. Its unit has no projected secret and cannot modify the backup roots.
Every supported native artifact writer sets the Core atomic-publication mode to
`0640`; the private default outside this deployment remains `0600`. This lets
the backup UID read complete atomically published research files through its
explicit `research-ops` supplementary group. DAC alone is not the writer
authorization boundary: the byte-attested systemd mount namespaces give Web,
Job, and migration disjoint writable subtrees and make every other service
read-only. No host account has a persistent `research-ops` membership.

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

Install the data steward's Ed25519 public keys and canonical trust store as
public, administrator-owned files. The corresponding private signing keys stay
outside this host and repository. The store schema is
`dataset_transformation_trust_store` version 1; every key entry binds its fixed
path, exact byte hash, validity interval, and optional revocation time/reason.
It may retain revoked keys for rejection evidence but must contain at least one
currently valid, non-revoked key. Set
`RESEARCH_DATASET_TRANSFORMATION_TRUST_STORE_HASH` to the SHA-256 of the exact
installed JSON bytes (including its single trailing newline):

```sh
sudo install -d -o root -g root -m 0755 \
  /etc/research-ops/dataset-transformation-keys
sudo install -o root -g root -m 0644 \
  /absolute/reviewed/steward.ed25519.pub \
  /etc/research-ops/dataset-transformation-keys/steward.ed25519.pub
sudo install -o root -g root -m 0644 \
  /absolute/reviewed/dataset-transformation-trust.json \
  /etc/research-ops/dataset-transformation-trust.json
sha256sum /etc/research-ops/dataset-transformation-trust.json
```

Preflight fixes both locations, rejects links/hardlinks or noncanonical bytes,
checks root ownership and exact modes, verifies store/key byte hashes and
Ed25519 encoding, and enforces store/key validity and revocation state. A
dataset manifest, API request, job payload, or local environment cannot add an
authority. Rotate by installing a reviewed new key and canonical store first,
updating the root-only runtime environment digest, and rerunning preflight;
record revocation in the store instead of deleting compromised key history.

Install the independent identity authority separately. Assertion schema v2 is
Ed25519-only; the issuer's private key remains on the external identity system
and is never installed or projected into a research service. The canonical
`independent_verifier_trust_store` version 1 binds one `authority_id`, store
issue/expiry times, and sorted unique public-key IDs, paths, content hashes,
validity intervals, and revocation records. Set
`RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_HASH` to the SHA-256 of the exact
installed JSON bytes, including its one trailing newline:

```sh
sudo install -d -o root -g root -m 0755 \
  /etc/research-ops/independent-verifier-keys
sudo install -o root -g root -m 0644 \
  /absolute/reviewed/verifier.ed25519.pub \
  /etc/research-ops/independent-verifier-keys/verifier.ed25519.pub
sudo install -o root -g root -m 0644 \
  /absolute/reviewed/independent-verifier-trust.json \
  /etc/research-ops/independent-verifier-trust.json
sha256sum /etc/research-ops/independent-verifier-trust.json
```

Preflight and the production loader enforce those exact paths and digest,
root-owned non-writable parent chains, `0644 root:root` single-link files,
canonical JSON/public-key encoding, validity, revocation, and stable descriptor
identity. A CLI caller cannot replace the path, digest, store, or public key.

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

The admitted Job worker resolves the import roots of the four installed
packages from the running interpreter and mounts those verified directories
read-only into each child. In the operated profile those roots must be under
the active `sys.prefix` and match installed distribution metadata; direct
package symlinks and `PATH` substitution are rejected. It never guesses a
checkout-style `.venv/src` directory. The Job unit permits only the mount,
user, IPC, PID, UTS, and network namespace types required by bubblewrap (the
child's network namespace has no host interfaces); cgroup namespaces remain
denied. It permits `AF_NETLINK` only so bubblewrap can configure that isolated
loopback. This unit alone leaves `ProtectKernelTunables` disabled because
bubblewrap must set the namespaced user-namespace limit while creating the
child; the unprivileged, capability-free parent then invokes
`--disable-userns --assert-userns-disabled`, so research code cannot create a
nested user namespace. Other units continue to deny all namespace creation
and protect kernel tunables. Preflight and the operated runner both use the
same absolute `/usr/bin/bwrap`, `/usr/bin/prlimit`, and `/usr/bin/timeout`.
CI installs the exact wheels outside the checkout and performs a real
bubblewrap import smoke test. Site acceptance must repeat
submit→claim→sandbox→receipt under the installed unit and PostgreSQL; static
unit parsing is not a substitute for that host qualification.

Run the filesystem qualifier for all five roles and install its path-redacted
receipt at `/etc/research-ops/filesystem-qualification.json`. The native unit
sandbox treats datasets as read-only, allows only Web to write the manifest
subtree, protects Web/project evidence from the Job worker with nested
read-only mounts, permits migration to write only public static assets, and
makes outbox, validator, alert, diagnostics, retention, and backup views of
research roots read-only. Preflight requires every mountpoint above to exist
with the exact owner, group, and mode before a service starts.

`RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH` is the shared append-only exposure
authority at `/srv/research/registry/final_holdout_authority.jsonl`. It must
never point into a per-job sandbox, artifact namespace, or report namespace.
Native installation pre-creates that ledger and its `.lock` inode as exact
`0660 root:research-ops` files and applies the Linux kernel append-only flag to
the ledger. Preflight and the operated storage boundary verify that flag; the
service has neither `CAP_LINUX_IMMUTABLE` nor a nested user namespace with
which to clear it. The Job worker and its sandbox receive write
access to those two files only, never to the authority directory or sibling
registries.
The parent Job worker reserves the manifest/dataset/holdout scope before
launch, passes only the content-addressed reservation and fence to the child,
and records pre-exposure aborts separately from activated exposures.
Ordinary operated validation may create only a `PRIMARY_CONFIRMATION`
reservation. A terminal replay is a separate
`INDEPENDENT_REPRODUCTION` purpose with a one-per-primary budget; it requires
the completed primary receipt/result plus a verified, time-bounded
`independent_verifier` assertion. Actor names, worker roles, and generic
registry payloads cannot request that purpose, and its activation and result
must exactly match the primary candidate and evidence bindings.

## Production PKI gate

Obtain separate server certificates for the employee DNS name and PostgreSQL
DNS name, the relevant issuing CA files, and the operations-client CA from the
site PKI owner. `generate-test-pki.sh` output is acceptance-only: the presence
of a `TEST_ONLY` marker rejects a production start.

Required private-key permissions are:

- proxy key: `0600 root:root`;
- PostgreSQL key: `0600 postgres:postgres`, or `0640 root:postgres`;
- database passwords, Django secret, htpasswd, backup signing key, control
  database URL, and service-alert endpoint URL source: `0600 root:root`;
- operated-execution capability key: exactly 32 random bytes, `0400 root:root`;
- backup/off-site public verification keys: `0644 root:root`;
- certificates: no group/other write permission.

Preflight requires exact root-only source ownership before any non-root unit
starts. systemd copies each required source into that unit's private credential
mount; application processes cannot read `/etc/research-ops/secrets`.
The Nginx drop-in similarly projects the htpasswd into its private runtime
directory for the unprivileged worker. Preflight also verifies file type, no
symlink, certificate age, chain, DNS/IP identity, and public-key match without
logging certificate or key contents. Production defaults require at least 30
days of remaining validity.

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
sudo /bin/sh -c '
  set -a
  . /etc/research-ops/runtime.env
  set +a
  exec /opt/research-platform/current/services/research_operations/deploy/native/bin/bootstrap-postgresql.sh
'
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

Install the reviewed dedicated Nginx main configuration and systemd drop-in.
The main configuration drops workers to `research-proxy`, includes only the
Research virtual host, and never grants the proxy the shared `research-ops`
filesystem group. The drop-in projects the public static tree read-only and
projects the operations password file through `LoadCredential`. Render the
virtual-host template to the exact configured path, validate, then restart:

Migration rejects symlinks, hard-linked files, and special files in the static
tree after `collectstatic`, then fixes directories to `0755` and regular files
to `0644` before syncing the filesystem. These are public adapter assets, not
research evidence; the Nginx projection remains read-only.

```sh
sudo install -o root -g root -m 0644 \
  /opt/research-platform/current/services/research_operations/deploy/native/nginx/nginx.conf \
  /etc/nginx/nginx.conf
sudo install -D -o root -g root -m 0644 \
  /opt/research-platform/current/services/research_operations/deploy/native/nginx/nginx.service.d/research-operations.conf \
  /etc/systemd/system/nginx.service.d/research-operations.conf
sudo /usr/bin/python3 \
  /opt/research-platform/current/services/research_operations/deploy/native/bin/render-nginx.py \
  --template /opt/research-platform/current/services/research_operations/deploy/native/nginx/research-operations.conf.template \
  --output /etc/nginx/conf.d/research-operations.conf \
  --server-name research.internal.corp
sudo nginx -t -c /etc/nginx/nginx.conf
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

Production requires a root-owned, mode-`0750`, group-`research-backup`
executable at `RESEARCH_OPS_OFFSITE_EXPORT_HOOK`. The backup service invokes it
as:

```text
HOOK export --backup-directory ABS --target-id ID --encryption METHOD \
  --encryption-key-id KEY_ID --receipt ABS_NEW_RECEIPT
```

The hook must encrypt before external transfer, verify the remote object, and
atomically create a mode-0640 JSON receipt owned by
`research-backup:research-backup`. The receipt
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
hook's private key remains outside this service and repository. The wrapper
verifies that signature and binds the receipt to the local signed manifest
before success. A hook, key, signature, or receipt failure fails the systemd
backup unit and must alert; it never claims off-site success.

The local signed set remains `.staging-<backup-id>` throughout upload. The hook
writes a unique dot-prefixed staged receipt; the wrapper validates its exact
owner, group, mode, signature, and manifest binding before a same-filesystem
no-replace link publishes `<backup-id>.json`. Only then does it no-replace
rename the local directory to `<backup-id>` and fsync both roots. Failed upload
attempts therefore remain non-final and recoverable, and retention ignores
their dot-prefixed names. `BACKUP_RESUME_ID=<backup-id>` reuses the exact local
set, starts a new receipt attempt after an upload failure, or re-verifies an
already published receipt before completing an interrupted directory rename.

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
Any finalized UUID-named set that fails verification makes the retention unit
exit nonzero after emitting its secret-free plan; an active `.staging-*` set is
not treated as finalized. `LEGAL_HOLD` never bypasses verification: an invalid
held final is reported in both held and incomplete inventories and still makes
the audit fail.
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

The target starts two outbox instances, one admitted job worker, the persistent
service-health alert worker, persistent validator, web service, diagnostics
API, and Nginx. Each credential tier uses the dedicated UID listed above;
`research-ops` is only a shared research-filesystem group. Web and diagnostics
use different socket-primary groups. The dedicated `research-proxy` worker is
the only member of both groups, and its reviewed main configuration is byte-
attested by preflight; another credential tier therefore cannot connect
directly and forge ingress-authentication headers. Credential assignment
is an exact allowlist:

```text
migrate       owner DB password, Django secret
web           runtime DB password, Django secret
outbox        runtime DB password, Django secret
job worker    runtime DB password, Django secret, execution capability
alert worker  runtime DB password, alert endpoint URL
validator     validator DB password, Django secret
backup        backup DB password, Django secret, backup signing key
ops API       diagnostics DB password
nginx         operations htpasswd
```

No application unit receives the owner, validator, or backup password unless
its declared role requires it. In particular, Web receives neither those
credentials nor the backup key, alert endpoint, control database URL, htpasswd,
or execution capability. Source paths remain root-only and are hidden from
application mount namespaces. `ProtectProc=invisible` hides other UIDs'
processes, while distinct UIDs prevent the credential values that the runtime
entrypoint must place in a child process environment from being read through
another tier's `/proc/<pid>/environ`. Multiple outbox instances deliberately
share one UID because they receive the same credential set. A process in a
given tier can still inspect that same tier, so a credential-bearing tier must
not run unrelated code.
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
