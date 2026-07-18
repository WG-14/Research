# Platform release checklist

Record commands, exact versions, timestamps, checksums, and evidence locations
in an immutable release acceptance record. An unchecked item is a failed
promotion gate.

## Source and workspace

- [ ] The checkout is clean, on the intended protected commit, with no conflict markers or untracked runtime/private material.
- [ ] The Git SHA is a 40-character lowercase hash and the release ID is unique and approved.
- [ ] `uv lock --check --offline` passes from the monorepo root.
- [ ] `scripts/platform bootstrap` installs all three distributions from the root frozen lock.
- [ ] Distribution names and versions in the three `pyproject.toml` files are the intended release set.
- [ ] Architecture checks confirm Research has no Web/Operations dependency, Web uses public Research contracts, and Operations uses the Web/Core facades.
- [ ] Residue scanning finds no credentials, private keys, certificates, databases, dumps, backups, runtime environment files, absolute retired source paths, or symlinks in tracked platform trees.

## Tests and static validation

- [ ] Focused tests for every changed boundary pass first.
- [ ] Pytest collection passes for Research, Web, and Operations.
- [ ] The Research full suite runs exactly once after focused checks and passes.
- [ ] Bubblewrap, `prlimit`, and `timeout` are installed; strategy package, network/filesystem isolation, timeout, memory, and output fault tests pass without skips.
- [ ] The Web full suite passes against supported PostgreSQL with all PostgreSQL-specific tests executed and zero unexpected skips.
- [ ] The Operations full suite passes against supported PostgreSQL with all integration tests executed and zero unexpected skips.
- [ ] Browser E2E covers login, authorization denial, CSRF, validation submission, review/approval separation, report access, and safe error projection through the supported stack.
- [ ] `scripts/platform lint` and `scripts/platform compile` pass.
- [ ] Native deployment tests and `systemd-analyze verify` pass.
- [ ] Nginx configuration, TLS proxy behavior, security headers, secure cookies, and operations mTLS positive/negative paths pass on the target profile.

## Build and manifest

- [ ] `scripts/platform build` runs from a clean checkout and produces exactly one provenance-bearing wheel and one sdist for each distribution.
- [ ] Archive inspection proves internal Name/Version metadata, complete package payload, component source digest, shared platform source digest, and embedded Git SHA for all six artifacts.
- [ ] A negative check confirms a renamed/fabricated artifact and a same-version artifact from another commit are rejected.
- [ ] Wheel/sdist contents contain the required templates, static files, Django migrations, and Operations SQL migrations, with no runtime/private material.
- [ ] Each wheel and sdist installs and imports in an isolated environment outside the checkout.
- [ ] The native release venv is created by `scripts/platform install-release` from the three exact manifest-bound wheels; no editable or source-directory installation is present.
- [ ] `tools/verify_installed_release.py` reports `VERIFIED` for that venv and binds installed payload/direct-wheel hashes to `release.json`.
- [ ] Console and module entrypoints expose the intended command sets.
- [ ] `tools/release_manifest.py` runs from the clean commit and writes `release.json` outside the source tree.
- [ ] The manifest binds the exact Git SHA, release ID, three component versions, six artifact hashes/sizes, root lock digest, web/Operations migration digests, native deployment digest, build digest, and release-bundle digest.
- [ ] The promoted manifest and checkout are root-owned and not writable by the service identity.
- [ ] `RESEARCH_OPS_GIT_SHA`, `RESEARCH_OPS_RELEASE_ID`, `RESEARCH_OPS_BUILD_DIGEST`, and the expected migration digest match the manifest.

## Database and upgrade/rollback

- [ ] Fresh installation applies all Django and Operations migrations and least-privilege grants.
- [ ] Upgrade from the selected prior release preserves users, permissions, jobs, audit intents, admission state, receipts, managed reports, and release provenance.
- [ ] Candidate web/API/workers reject missing or mixed release identity.
- [ ] The prior application release can be restored according to the approved rollback plan without an unsafe live schema downgrade.
- [ ] Forward-fix and restore thresholds, decision authority, and maximum rollback window are recorded.

## Official native deployment

- [ ] `services/research_operations/deploy/OFFICIAL_DEPLOYMENT` selects `native-systemd`.
- [ ] The Compose files are labelled non-official and were not used as acceptance evidence.
- [ ] A fixed non-login `research-ops` identity and immutable release layout are installed.
- [ ] Production `runtime.env` has no placeholders and is mode `0640` with the documented owner/group.
- [ ] `RESEARCH_RUNTIME_PROFILE=operated`; direct Research CLI execution fails closed while an authorized admitted job succeeds.
- [ ] Preflight passes against the actual release, external paths, owners, PKI, secrets, storage receipt, off-site policy, retention, RPO, and RTO.
- [ ] Web, operations API, two outbox workers, admitted job worker, and validator start under systemd and report the same release.
- [ ] SIGTERM drains within the declared timeout; crash restart and host reboot recover without duplicate publication.
- [ ] The admitted job dispatcher runs in a supervised spawn child; forced child timeout, memory death, invalid output, and crash fail only that fenced job and the parent accepts later work.
- [ ] Database-down, validator-stale, worker-stale, outbox-failure, receipt-pending, backup-fence, and release-mismatch conditions close the appropriate readiness endpoint and recover after the dependency is restored.

## Security and organization-owned gates

- [ ] Service, security, data, on-call, incident-command, backup, and recovery-approval owners are real stable directory identities.
- [ ] Service/security and backup/recovery-approval separation constraints pass.
- [ ] Organization PKI issued the employee server, PostgreSQL server, and operations-client material; no test marker is present.
- [ ] Certificate identity, chain, key match, ownership/mode, remaining lifetime, renewal, revocation, and expiry alerts are verified.
- [ ] Production secrets are external, least-readable, rotated or newly issued as required, and absent from logs/diagnostics/audit/build artifacts.
- [ ] Alert routes exist for systemd failures, preflight, readiness, certificates, workers, outbox/DLQ, backups, off-site export, and restore drills.

## Backup, off-site retention, and recovery

- [ ] A fenced backup drains mutable work and pending audit/receipt state before sealing.
- [ ] The PostgreSQL dump and all declared external evidence roots produce a signed, release-bound manifest that verifies independently.
- [ ] The root-owned off-site hook encrypts before transfer, verifies the remote object, and creates a bound immutable receipt.
- [ ] Retention and legal-hold policy is approved; dry-run inventory preserves the required minimum complete copies.
- [ ] A blank-database and blank-filesystem restore verifies signatures, hashes, registries, audit, migration binding, and no-follow path rules.
- [ ] Isolated post-restore checks cover authentication, managed report access, admitted work, worker competition, exactly-once audit projection, and readiness.
- [ ] Explicit recovery activation and idempotent replay are verified; no restore is performed over the active namespace.
- [ ] Measured backup age and restore duration satisfy the approved RPO and RTO.

## Promotion

- [ ] The acceptance record identifies every test environment and clearly marks any scope not tested (multi-host, failover, storage power-loss, or container execution).
- [ ] No source-only or fixture-only result is presented as organization/site acceptance.
- [ ] Service, security, data/evidence, backup, and recovery approvers sign the residual-risk record.
- [ ] Remote CI for the exact commit is green and artifact checksums match the locally reviewed manifest.
- [ ] A human performs tag and promotion actions; this checklist does not create or push them.
