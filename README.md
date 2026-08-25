# Market Research Platform

This repository is the Git monorepo for an offline, reproducible market
research platform. It contains the research engine, its authenticated internal
web adapter, and the operations trust domain needed to run that web adapter.
It is not a trading bot: account access, private exchange APIs, order or fill
ingestion, order submission, and runtime trading controls are outside scope.

## Distributions

| Distribution | Source | Responsibility |
| --- | --- | --- |
| `market-research` | `src/market_research` | Framework-neutral research engine, deterministic CLI, artifacts, governance, and public application contracts. |
| `market-research-internal-web` | `apps/internal_web` | Django authentication, RBAC, CSRF protection, browser workflows, safe projections, and web metadata. |
| `research-operations` | `services/research_operations` | PostgreSQL coordination, durable workers, health/readiness, audit projection, release admission, backup/recovery, and deployment assets. |

All three packages share the root `uv.lock`. The dependency direction is
strictly one way:

```text
research-operations
  -> market_research_web.operations_contract
  -> market_research.application / adapter_contracts / platform_contracts

market-research-internal-web
  -> market_research.application / adapter_contracts / platform_contracts
  -> published read-only research query contracts

market-research
  -X-> web or operations packages
```

The web package does not reimplement Research rules: mutations use application
contracts and authenticated exploration uses the published, read-only Research
query boundary. Operations reaches web behavior only through
`market_research_web.operations_contract`. See
[`docs/monorepo-architecture.md`](docs/monorepo-architecture.md) for the full
boundary. The exact import modules in
[`docs/architecture-boundaries.json`](docs/architecture-boundaries.json) are
the executable allowlist enforced against every production Python module; a
listed package does not implicitly authorize its submodules.

## Workspace commands

Python 3.12 and `uv` are the supported workspace baseline.

```sh
scripts/platform bootstrap
scripts/platform test-core
scripts/platform test-web
scripts/platform test-operations
scripts/platform test-all
scripts/platform test-browser
scripts/platform test-integration
scripts/platform lint
scripts/platform typecheck
scripts/platform compile
scripts/platform docs-check
scripts/platform verify-complete --help
scripts/platform verify-product-scope --help
scripts/platform verify-multi-asset-audit --json
scripts/platform verify-multi-asset-audit-result
scripts/platform audit
scripts/platform build
scripts/platform install-release --help
scripts/platform verify-deployment
scripts/platform backup-restore-drill --help
scripts/platform research --help
```

`bootstrap` performs a frozen install of every package and dependency group
from the root lock. The package-specific test commands remain available when a
change affects only one trust domain.

`test-integration` requires the `INTERNAL_WEB_DATABASE_*` settings and
`RESEARCH_OPS_TEST_DATABASE_URL` to describe the same disposable PostgreSQL
database. It verifies that identity, applies the shared Web migrations needed
by Operations foreign keys, and then runs the Web and Operations concurrency
contracts.

`verify-complete` is bound to the current July 2026 investment-research
platform rubric supplied for this review by source and instruction SHA-256. It
validates exactly 184 A--J criteria and 12 fatal gates in the
[canonical audit matrix](docs/investment-research-platform-audit.json). For
this platform-completeness review, that A--J matrix is the single evaluation
authority. A file, type, document, command string, or unexecuted test never
earns `VERIFIED` or fatal-gate `PASS`.

The normalized, reviewable copies of the exact current
[audit rubric](docs/investment-research-platform-audit-rubric.md) and
[execution instructions](docs/investment-research-platform-audit-instructions.md)
are hash-checked by the validator. The evaluated identity is a deterministic
source-surface hash. The recorded Git SHA is generation provenance only, which
avoids an impossible self-reference between a commit and a checked-in report
that contains that commit.

Generate the conservative matrix, run its exact hash-bound evidence suite,
then regenerate and validate the reports with:

```sh
uv run --frozen --no-sync --package market-research python tools/update_reference_audit.py
uv run --frozen --no-sync --package market-research python tools/reference_audit_receipt.py
uv run --frozen --no-sync --package market-research python tools/update_reference_audit.py
uv run --frozen --no-sync --package market-research python tools/render_reference_audit_report.py
scripts/platform verify-complete --json
```

The receipt command executes every exact pytest file referenced by the matrix,
requires zero failures, errors, and skips, and rejects source changes during
the run. Without a current receipt the generated matrix remains structurally
valid but caps implementation claims at M3, marks otherwise passing fatal
gates `UNVERIFIED`, and cannot be COMPLETE.

The 140-row multi-asset expansion (`verify-multi-asset-audit`) and 431-row
derivative/product expansion (`verify-product-scope`) remain historical or
product-specific evidence. They are not the completion authority for this
A--J review.

The implemented common contracts and their fail-closed data flow are described
in [multi-asset research contracts](docs/multi-asset-research.md).
The conservative current assessment is available as the
[184-criterion audit report](docs/investment-research-platform-audit-report.md)
and the corresponding
[machine-readable result](docs/investment-research-platform-audit-result.json).
Both are generated deterministically and checked in CI with
`verify-complete --validate-structure`.

## Research CLI

For a researcher-controlled offline workstation, the canonical command is the
deterministic workspace wrapper:

```sh
scripts/platform research <command>
```

It fixes Python hash seeding and all six supported numerical backend thread
counts before Python starts; strict receipts independently verify those values.

The supported strategy set is exactly:

- `sma_with_filter`
- `buy_and_hold_baseline`
- `noop_baseline`
- `threshold_research_only`

Each strategy is a hash-bound package with a strict sidecar manifest, complete
parameter and hypothesis metadata, automatic failure-isolated discovery, and a
common decision/result contract. See
[`docs/strategy-development.md`](docs/strategy-development.md) for the
add/validate/approve/retire workflow. Multi-manifest jobs use a network-denied,
read-only Linux process sandbox; operated jobs execute in supervised child
processes so a strategy timeout or memory failure does not take down the
control plane.

The CLI consumes externally prepared immutable datasets. Authoritative dataset
freeze is a separate data-administration action: the production API accepts
only an `operated` runtime with an exact root-owned transformation trust store
and root-owned Ed25519 public keys. Neither a provenance manifest nor a CLI
argument can select that authority. The public `research-freeze-dataset`
command therefore fails closed on an ordinary researcher workstation, while
direct CLI bootstrap is itself disabled on an operated service host; an
admitted data-administration adapter must call the same freeze API there.

After an administrator publishes the immutable artifact manifest, a typical
local research workflow is:

```sh
scripts/platform research research-readiness \
  --manifest /abs/experiment.json --json
scripts/platform research research-backtest \
  --manifest /abs/experiment.json
scripts/platform research research-walk-forward \
  --manifest /abs/experiment.json
scripts/platform research research-validate \
  --manifest /abs/experiment.json
```

Futures and options deliberately use a separate product-semantic authority;
they are never admitted to the spot-candle engine. Their immutable chain,
simulation and evidence-bundle workflow is documented in
[`docs/derivative-research.md`](docs/derivative-research.md).

Replay a receipt with the same deterministic launcher:

```sh
scripts/platform research research-reproduce-run \
  --manifest /abs/experiment.json \
  --receipt /abs/reports/experiment-id/reproduction-receipt.json \
  --out /abs/reports/reproduction-result.json
```

The command exits zero only for `status=PASS`; drift and invalid baselines exit
nonzero. The comparison document is written to `--out` (or beneath the
configured external report root when omitted), and records the isolated
reproduced report and receipt paths plus exact drift rows.

### Portable classic research package

An official classic result, its exact reproduction receipt and experiment
manifest can be promoted from a symbolic recipe to one deterministic `.mrpkg`
archive. The archive carries the research summary, hypothesis, data/code/
environment/parameter manifests, result index, validation evidence,
limitations, reproduction plan, artifact sidecar, and a hash for every member.
It never carries credentials, private keys, trust-store authority, or a private
verifier assertion.

Dataset transport is explicit and fail closed:

- `included` embeds the frozen SQLite bytes only when the result is bound to a
  canonical `RESEARCH_PACKAGE_EXPORT=ALLOW` decision for
  `INTERNAL_RESEARCH_PACKAGE` and the governing license permits derivative
  retention;
- `external_content_addressed` embeds no dataset bytes and records the exact
  sidecar, content, schema, identity and manifest hashes. Verification and
  replay then require both the byte-identical sidecar and frozen SQLite file as
  explicit arguments.

```sh
market-research research-build-portable-package \
  --result /abs/reports/experiment-id/backtest_report.json \
  --manifest /abs/experiment.json \
  --receipt /abs/reports/experiment-id/reproduction_receipt.json \
  --dataset-mode external_content_addressed \
  --out /abs/packages/experiment-id.mrpkg

market-research research-verify-portable-package \
  --package /abs/packages/experiment-id.mrpkg \
  --external-artifact-manifest /abs/frozen/artifact.manifest.json \
  --external-dataset /abs/frozen/candles.sqlite \
  --out /abs/receipts/package-verification.json

market-research research-reproduce-portable-package \
  --package /abs/packages/experiment-id.mrpkg \
  --external-artifact-manifest /abs/frozen/artifact.manifest.json \
  --external-dataset /abs/frozen/candles.sqlite \
  --workspace /abs/empty-replay-workspace \
  --out /abs/receipts/package-reproduction.json
```

Cold replay refuses a baseline that was not created by a non-editable installed
`market-research` distribution. It executes the real CLI twice under
`python -I` from empty CWD, HOME and cache roots, verifies that the imported
module is owned by the installed distribution, and requires exact fingerprint
agreement. A package containing validated evidence remains explicitly
non-promotable offline: canonical registry/principal authority, public trust,
the signed external verifier assertion and shared one-use holdout authority are
external prerequisites, never inferred from caller-supplied JSON.

Publishing an authoritative independent-verification result additionally
requires a cryptographically authenticated, time-bounded principal assertion:

```sh
scripts/platform research research-reproduce-run \
  --manifest /abs/experiment.json \
  --receipt /abs/reports/experiment-id/reproduction-receipt.json \
  --verification-id independent-check-2026-07-29 \
  --verification-version 1 \
  --verifier-assertion /abs/identity/assertions/independent-check-2026-07-29.json \
  --out /abs/reports/reproduction-result.json
```

The external issuer signs assertion schema v2 with an Ed25519 private key that
is never installed on the research host. Its
issuer, key ID, authenticated subject, roles, authentication/expiry times,
nonce, audience, and exact verification scope are hash-bound. The scope
includes the verification identity, experiment and research versions, source
report hash, and baseline receipt hash. In the operated profile the sole trust
store is `/etc/research-ops/independent-verifier-trust.json`; its exact byte
digest comes from the root-owned runtime environment. The canonical store
binds one authority and each Ed25519 public key's fixed path, content hash,
validity interval, and revocation state. Both it and public keys are
`0644 root:root`, single-link files beneath root-owned, non-writable directory
chains. The verifier rejects path overrides, unknown/legacy assertion schemas,
HMAC assertions, expired or future authority material, revoked keys, link
aliases, content substitution, and mutation during descriptor reads. Public
keys and their paths are never copied into research artifacts. A caller-supplied
`--verifier` value is only a display alias. Supplying an alias without
`--verifier-assertion` retains diagnostics as explicitly non-authoritative
`RESEARCH_ONLY` output and cannot satisfy an approval gate.

For a validated terminal result, the signed mode is also the only mode allowed
one final-holdout replay. The shared authority records it as
`INDEPENDENT_REPRODUCTION`, binds the completed primary confirmation/result and
validated-result receipt together with the assertion subject, scope hash,
content hash, and nonce, and consumes the one-replay budget at reservation.
The ordinary `PRIMARY_CONFIRMATION` purpose, a caller-supplied verifier alias,
or a generic actor/registry payload cannot be relabeled into this exception.

Non-POSIX hosts are rejected until their identity adapter provides equivalent
no-follow, ownership, link-count, byte-pinning, and descriptor-stability checks.

Validation-bound studies advance through immutable, evidence-backed states:
`IDEA → STRUCTURED → EXPLORATORY → PREREGISTERED → VALIDATING`, followed by
`VALIDATED`, `REJECTED`, or `INCONCLUSIVE`. A validated study may enter the
published `ProspectiveValidationApplicationService`, which freezes rules before
the observation period, records only simulated fills and actual arrival/missing
evidence, publishes a research conclusion, and creates a final versioned
Research Package whose explicit references bind the run, snapshot, Feature
definitions, validation decision, prospective stream, conclusion, and
reproduction receipt. These are research conclusions only and never authorize
capital deployment.

The freeze command accepts only provenance schema 4, including its complete
hash-bound external source catalog, physical source/stage byte bindings, and
raw → cleaned → standardized transformation receipts with verified external
code and configuration bytes. Legacy provenance is not translated. It prints
the generated schema-4 `artifact_manifest_uri` and
`artifact_manifest_hash`. Bind both exact values into the experiment manifest
and set `dataset.source=frozen_sqlite_candles`. The mutable
`dataset.source=sqlite_candles` compatibility path is exploratory only and
cannot produce an authoritative reproduction receipt.

On an operated service host, `RESEARCH_RUNTIME_PROFILE=operated` disables the
direct `market-research` entrypoint with a fail-closed exit. Jobs must enter
through the authorized Operations admission and fencing path. This gate
prevents the installed service profile from bypassing operational admission;
it does not turn research output into trading permission.

## External state

No runtime state belongs in this checkout. The following settings must resolve
to absolute repository-external locations:

- `RESEARCH_DATA_ROOT`: immutable or externally prepared datasets;
- `RESEARCH_ARTIFACT_ROOT`: derived artifacts and managed static output;
- `RESEARCH_REPORT_ROOT`: research and operator-readable reports;
- `RESEARCH_CACHE_ROOT`: disposable cache;
- `RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH`: append-only experiment identity authority;
- `RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH`: shared append-only final-holdout reservation and exposure authority;
- `RESEARCH_DB_PATH`: optional local SQLite candle input for commands that require it.

Production PostgreSQL, credentials, certificates, private keys, backups,
off-site receipts, restored namespaces, and logs also remain outside Git.
`ResearchSettings` and `ResearchPathManager` are the canonical Research path
boundary. Datasets are immutable inputs; derived files use atomic publication,
and evidence streams use append-only hash chains.

## Workspace build and release identity

Build all three distributions from one clean commit:

```sh
scripts/platform build
scripts/platform release-manifest \
  --release-id platform-YYYY.MM.DD.N \
  --artifacts-dir "$PWD/dist/platform" \
  --output /absolute/release-staging/release.json
```

The build command and generator refuse a dirty checkout. `build` creates a
temporary `git archive` of `HEAD`, injects canonical build provenance, and
builds from that immutable snapshot rather than from the developer working
tree. The generator opens every archive and rejects it unless its package
metadata, complete package payload, and embedded provenance match that exact
checkout. It binds:

- the 40-character Git SHA and release ID;
- all three distribution names and versions;
- every wheel and sdist filename, size, and SHA-256 digest;
- the unified `uv.lock` digest;
- Django and Operations migration counts, latest revisions, and digests;
- the official native-deployment digest;
- aggregate build and release-bundle digests.

Every wheel and sdist contains a canonical `_build_provenance.json` with its
distribution/version, Git SHA, component source digest, and a shared platform
source digest. This prevents artifacts from two commits that happen to use the
same `0.1.0` package version from being combined. A filename with arbitrary or
well-formed-but-different bytes is not release evidence.

The promoted `release.json` must be root-owned, immutable to the service user,
and match `RESEARCH_OPS_GIT_SHA`, `RESEARCH_OPS_RELEASE_ID`, and
`RESEARCH_OPS_BUILD_DIGEST`. Worker heartbeats and readiness checks reject a
mixed or missing release identity.

## Deployment status

The only official deployment profile is
[`services/research_operations/deploy/native`](services/research_operations/deploy/native):
PostgreSQL 16, Nginx, Gunicorn, and systemd on one qualified Linux host.
`services/research_operations/deploy/compose.yaml` is a non-official portability
reference and is not deployment acceptance evidence.

The checked-in profile implements fail-closed preflight, service supervision,
durable workers, health/readiness, backup fencing, signed backup metadata,
blank-namespace recovery verification, dry-run retention auditing, and an
encrypted off-site export hook contract. A site must still provide and approve
all external operating inputs before promotion:

- named service, security, data, on-call, incident, backup, and recovery owners;
- organization-issued server, database, and operations-client PKI plus renewal and revocation procedures;
- an independently installed encrypted off-site export implementation and destination;
- approved retention, legal-hold, RPO, and RTO policy;
- alert routing, scheduled restore drills, host/storage qualification, and release-specific acceptance evidence.

Repository tests and example preflight do not satisfy those organization-owned
gates. Do not describe a release as production-ready until the release
checklist and site runbook have evidence for the actual host and release.

## Further documentation

- [`docs/monorepo-architecture.md`](docs/monorepo-architecture.md): trust domains, authorities, and dependency rules
- [`docs/internal-web-architecture.md`](docs/internal-web-architecture.md): web capabilities and security contract
- [`docs/internal-web-operations-handoff.md`](docs/internal-web-operations-handoff.md): operator ownership and runbook handoff
- [`docs/research-data-dictionary.md`](docs/research-data-dictionary.md): generated canonical dataset field semantics and ownership
- [`docs/research-standard-authority.md`](docs/research-standard-authority.md): strict observation-to-hypothesis manifest, admission, lifecycle, and package binding
- [`docs/strategy-development.md`](docs/strategy-development.md): strategy package authoring, validation, isolation, and retirement
- [`docs/monorepo-iterations.md`](docs/monorepo-iterations.md): consolidation record and remaining gates
- [`docs/release-checklist.md`](docs/release-checklist.md): release and promotion evidence checklist
- [`services/research_operations/deploy/native/README.md`](services/research_operations/deploy/native/README.md): official deployment procedure
- [`services/research_operations/docs/runbook.md`](services/research_operations/docs/runbook.md): backup, recovery, and incident procedures
