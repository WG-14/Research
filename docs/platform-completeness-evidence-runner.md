# Platform completeness evidence runner

`tools/platform_completeness.py` evaluates receipts by default. Its opt-in
runner executes the checked-in criterion template only after every evidence
path, path hash, command, and null receipt placeholder passes a fail-closed
preflight.

The evidence directory must be an absolute, repository-external, new or empty
directory. Do not put receipts, logs, or resolved manifests in Git.

```console
scripts/platform verify-complete \
  --run-evidence \
  --manifest docs/platform-completeness-criteria.json \
  --evidence-root /absolute/external/platform-completeness-run \
  --timeout-seconds 1800
```

The runner accepts only explicit test modules under the repository's three
owned test roots through `.venv/bin/pytest`, or an evidence-safe, argument-free
verification subcommand of `scripts/platform`. It invokes commands with a fixed
repository cwd, an argv sequence and `shell=False`. Pytest selection-changing
options, path traversal, arbitrary executables, mutating platform subcommands,
non-null input receipt hashes, stale evidence hashes, and repository-internal
output roots are rejected before execution.

Identical argv sequences are executed once. Each criterion still receives a
separate receipt that binds its criterion ID, command ID, rubric hash, evidence
path hashes, repository commit and source-state hash to the shared command log
hashes. Dangerous Python/pytest environment injection variables are removed.
Secret-like environment values are redacted from the ledger and replaced in
captured stdout and stderr before the log hashes are computed.

Runner-issued repository verification is capped at E4. An E5 criterion remains
failed even when all repository tests pass: E5 requires a separately produced,
repository-external site or organization attestation with a bound attestation
file, issuer, site identity, and issuance time. A pytest or `scripts/platform`
receipt that claims E5 is rejected rather than promoted.

For pytest evidence, exit status zero is not sufficient. The captured summary
must contain at least one passed test and must contain no skipped, xfailed,
xpassed, deselected, zero-collected, or “no tests ran” outcome. A disqualified
summary is recorded in the receipt and ledger, but the receipt is issued at E0
and the criterion fails closed. This prevents missing browser, PostgreSQL, or
other integration prerequisites from being mistaken for successful evidence.

The external bundle contains:

```text
platform-completeness-run/
├── logs/
│   ├── <command-group>.stdout.log
│   └── <command-group>.stderr.log
├── receipts/
│   └── <criterion>.json
├── resolved-manifest.json
├── validation-ledger.json
└── validation-ledger.md
```

`resolved-manifest.json` is a copy of the input template with receipt SHA-256
values filled in; the checked-in template remains unchanged with null receipt
hashes. Re-evaluate a completed bundle without running commands:

```console
scripts/platform verify-complete \
  --manifest /absolute/external/platform-completeness-run/resolved-manifest.json \
  --evidence-root /absolute/external/platform-completeness-run
```

Exit status `0` means all strict completion criteria passed. Status `1` means
the evidence run finished but the resolved manifest remains incomplete. Status
`2` means the manifest, output root, command grammar, or runner invocation was
rejected.
