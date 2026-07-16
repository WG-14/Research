# Internal Research Web Adapter

This is an isolated, server-rendered Django adapter over the offline
`market-research` application services. It is a development and integration
deliverable, not an approved production deployment.

The adapter never submits orders, accesses accounts, collects network market
data, or accepts an arbitrary server path from a browser. Authoritative research
artifacts remain under repository-external roots managed by
`ResearchSettings` and `ResearchPathManager`.

## Implemented workflow

An authorized runner can sign in, upload a bounded Research Semantics v2 JSON
manifest, queue a combined readiness/workload preflight, submit validation only
after a hash-bound `PASS`, inspect progress and outcomes, and download a
redacted projection with both its own hash and the authoritative source-result
hash. Authorized users can catalog and compare two to ten canonical decision
reports produced by visible completed web validations; every list and compare
operation rechecks the source bindings and hashes. Reviewers can record change
requests or rejection against a hash-verified `PASS` result. A distinct
`research_approver` can record final approval only after current-password
confirmation, registry/lifecycle validation, unresolved-requirement checks, and
originator/prior-reviewer separation. Arbitrary governance transitions,
historical CLI report discovery, reproduction, retry, and state repair remain
disabled.

Login failures are throttled using database-backed secret-HMAC account and
source-address subjects. Web manifest metadata globally reserves each
`experiment_id`, preventing different web users from targeting the same
experiment-scoped core output. Job and manifest ORM changes commit with an
immutable database audit intent; the external JSONL projection is checked
separately and a failed projection remains visible as pending evidence.

## Development setup (WSL/Linux only)

From `apps/internal_web`, configure absolute external research roots and a
development-only web secret. The selected immutable dataset or SQLite input
must already exist; the web adapter does not collect or backfill it.

```bash
export RESEARCH_DATA_ROOT=/absolute/external/research/datasets
export RESEARCH_ARTIFACT_ROOT=/absolute/external/research/artifacts
export RESEARCH_REPORT_ROOT=/absolute/external/research/reports
export RESEARCH_CACHE_ROOT=/absolute/external/research/cache
export RESEARCH_DB_PATH=/absolute/external/research/input.sqlite
export INTERNAL_WEB_SECRET_KEY='replace-with-a-development-secret'
export INTERNAL_WEB_SECURE_SSL_REDIRECT=false
export INTERNAL_WEB_SECURE_COOKIES=false

uv sync --dev
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver --insecure 127.0.0.1:8000
```

The two `false` security overrides and `--insecure` are suitable only for this
loopback HTTP development server. Do not expose it to an internal network.
Production-like HTTPS environments must keep secure cookies and TLS redirect
enabled. Assign ordinary accounts to one of the migrated groups:
`research_viewer`, `research_runner`, `research_reviewer`,
`research_approver`, or `research_admin`.

The repository intentionally provides no persistent worker service or worker
management command. Integration tests exercise `portal.worker.run_worker_once`
directly. A submitted development job will remain queued unless a developer
invokes that function in a controlled test. An expired running job is never
automatically repaired or retried.

The web adapter rejects a raw manifest before core parsing when it exceeds the
default admission limits of 4,096 parameter candidates, 32 execution scenarios,
or 32,768 candidate/scenario work units. The positive-integer settings
`INTERNAL_WEB_MAX_PARAMETER_CANDIDATES`,
`INTERNAL_WEB_MAX_EXECUTION_SCENARIOS`, and `INTERNAL_WEB_MAX_WORK_UNITS` may
only be changed as an explicit server-side policy; browser users cannot
override them. The CLI research contract is not changed by these web limits.

The login-throttle settings default to five failures in 900 seconds followed by
a 900-second block. `INTERNAL_WEB_LOGIN_FAILURE_LIMIT`,
`INTERNAL_WEB_LOGIN_FAILURE_WINDOW_SECONDS`, and
`INTERNAL_WEB_LOGIN_BLOCK_SECONDS` accept only bounded positive ASCII integers.
These controls use the same development SQLite metadata database and are not
evidence of supported multi-host enforcement.

## Verification

```bash
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run python manage.py check \
  --settings=market_research_web.settings_test
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run python manage.py makemigrations \
  --check --dry-run \
  --settings=market_research_web.settings_test
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -q -s
```

WSL can inherit Windows `TEMP`/`TMP` values under `/mnt/c`. Python
`multiprocessing` and pytest temporary I/O are not reliable on that filesystem,
so validation commands deliberately use the Linux `/tmp` filesystem.

The browser test requires Chromium and its Linux libraries:

```bash
uv run python -m playwright install --with-deps chromium
TMPDIR=/tmp TEMP=/tmp TMP=/tmp INTERNAL_WEB_REQUIRE_BROWSER_E2E=1 \
  uv run pytest -q -s \
  tests/test_browser_e2e.py
```

## Windows access and operational boundary

There is no supported internal-network URL in this repository. After a
separately authorized operational project supplies a supported database,
worker supervisor, TLS/reverse proxy, identity lifecycle, monitoring,
backup/restore, rollback, and incident procedures, Windows users can open that
HTTPS URL in Edge and install it as an app/shortcut. Until those gates exist,
this adapter must not be described or used as a long-term operated service.

SQLite multi-user/multi-worker behavior, concurrent multi-row final approval,
automatic audit projection recovery, and safe reproduction remain explicitly
unproven. The report catalog does not scan arbitrary paths or infer authority
from unindexed legacy CLI artifacts.

See `docs/internal-web-architecture.md` and
`docs/internal-web-iterations.md` for the decision record, verified scope, and
residual risks.
