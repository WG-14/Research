# Research data dictionary

The authoritative field dictionary is the generated, machine-readable
[`research-data-dictionary.json`](generated/research-data-dictionary.json).
Its source is
`market_research.research.datasets.schema_dictionary`; do not edit the JSON by
hand.

Every field records its name, type, unit, meaning, nullability, valid range,
generation method, causal availability time, provider, versioned change
history, and owning module. The dictionary covers the immutable canonical
candle table, every per-source field in source provenance v2, point-in-time
universe membership and attribute versions, market-calendar authority, and
corporate-action raw-to-adjusted transformation evidence.

Point-in-time universe queries always take both an economic effective date and
a knowledge cutoff. Inactive and delisted members and all correction versions
remain in the artifact; a later correction cannot alter an earlier as-of
result. Universe and calendar `source_uri` values identify absolute local,
repository-external artifacts, with content and schema hashes bound into the
manifest, dataset query evidence, and research report.

Session calendars distinguish `continuous_24x7` from scheduled markets. A
scheduled authority declares an IANA timezone, reviewed tzdb version, weekly
local sessions, holidays, early closes, publication/observation times, and the
fail-closed DST policy. No runtime source probing is performed.

Corporate-action transformation evidence preserves raw input and adjusted
output row hashes plus an event-by-event before/after hash chain. Split ratios
mean post-action units per pre-action unit. Backward total-return dividends use
the prior raw close, and observations on or after a causally known delisting or
liquidation are rejected rather than synthesized.

Regenerate after an approved schema-contract change:

```bash
uv run --package market-research python tools/check_dataset_dictionary.py --write
scripts/platform docs-check
```

The normal documentation check compares the generated document byte-for-byte
with the code contract. Unknown legacy provenance fields and legacy schema
versions are rejected; they are not silently translated.
