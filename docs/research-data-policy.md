# Research artifact policy

Research artifacts are diagnostic and reproducibility evidence. They must not
be written into the repository. Dataset snapshots and derived traces are
separate from operator-readable reports; audit streams are append-only JSONL.
Use the `ResearchPathManager` and atomic storage helpers for every output.

## Authoritative dataset inputs

Authoritative runs use artifact-manifest schema 3 and
`dataset.source=frozen_sqlite_candles`. The artifact identity binds complete
OHLCV content, physical schema, exact scope, and a strict source-provenance
manifest. That provenance records every source and its priority, acquisition
request parameters, request and receipt times, provider response version,
external preparation-code version, retry count, complete/partial/failed status,
error code, coverage, upstream checksum, supported market semantics, and the
ordered raw, cleaned, and standardized lineage stages. Secret-like request
parameter names are rejected. Partial or failed source records may be retained
as provenance evidence but cannot be promoted into an authoritative frozen
artifact.

The frozen-candle source-provenance v2 scope remains deliberately narrow:
single-instrument spot data, UTC, and a continuous 24x7 observation calendar.
Price adjustment, corporate actions, and point-in-time universe membership are
`not_applicable` to that physical artifact schema, so a provenance manifest
claiming broader physical contents still fails closed.

Reviewed domain contracts may accompany a research manifest separately. A
point-in-time universe retains inactive/delisted members and every corrected
version, and requires both an effective date and an observation-time cutoff.
A calendar authority covers either continuous 24x7 or explicit sessions using
an IANA timezone, tzdb version, holidays, early closes, and a fail-closed DST
policy. Corporate-action evidence binds event, publication, and observation
times and hashes exact raw and adjusted rows before and after each applied
split or dividend; known post-delisting rows are rejected. These inputs are
externally prepared immutable local artifacts, never network discoveries.

Those contracts are hash-bound into manifest, dataset-query, readiness, and
report evidence. They do not make the current single-instrument candle
artifact a multi-instrument universe store. Validation-bound session-market,
adjusted-price, or multi-instrument frozen artifacts still require a reviewed
artifact/provenance schema extension; unsupported combinations fail closed.

Missing or non-finite OHLCV values are rejected. In particular, a missing
volume is never converted to real zero volume. Mutable SQLite remains an
explicit exploratory compatibility source and cannot produce an authoritative
reproduction receipt.

Artifact-manifest schema 2 has no source provenance and is read-only legacy.
There is no automatic migration: refreeze the original external input with a
valid provenance manifest to create a new schema-3 artifact.
