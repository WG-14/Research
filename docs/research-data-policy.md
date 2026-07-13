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
time and coverage, upstream checksum, supported market semantics, and the
ordered raw, cleaned, and standardized lineage stages.

The supported data scope is deliberately narrow: single-instrument spot data,
UTC, and a continuous 24x7 observation calendar. Price adjustment, corporate
actions, and point-in-time universe membership are `not_applicable` in this
scope. A provenance manifest claiming equities, exchange sessions, adjusted
prices, corporate actions, or a point-in-time universe fails closed. Support
for those domains requires new reviewed artifact contracts rather than an
option on the candle adapter.

Missing or non-finite OHLCV values are rejected. In particular, a missing
volume is never converted to real zero volume. Mutable SQLite remains an
explicit exploratory compatibility source and cannot produce an authoritative
reproduction receipt.

Artifact-manifest schema 2 has no source provenance and is read-only legacy.
There is no automatic migration: refreeze the original external input with a
valid provenance manifest to create a new schema-3 artifact.
