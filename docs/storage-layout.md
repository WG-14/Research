# Research storage layout

This repository stores code and examples only. All runtime research data is
repository-external and configured through `ResearchSettings`.

- datasets: `RESEARCH_DATA_ROOT`
- derived experiment artifacts: `RESEARCH_ARTIFACT_ROOT/derived/`
- reports and validation summaries: `RESEARCH_REPORT_ROOT/`
- disposable cache: `RESEARCH_CACHE_ROOT`

Use SQLite for candle inputs, atomic writes for JSON reports, and append-only
JSONL for audit streams. Paths must be absolute and outside the repository.
