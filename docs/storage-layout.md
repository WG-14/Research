# Research storage layout

This repository stores code and examples only. All runtime research data is
repository-external and configured through `ResearchSettings`.

- datasets: `RESEARCH_DATA_ROOT`
- derived experiment artifacts: `RESEARCH_ARTIFACT_ROOT/derived/`
- reports and validation summaries: `RESEARCH_REPORT_ROOT/`
- disposable cache: `RESEARCH_CACHE_ROOT`

Use SQLite for candle inputs, atomic writes for JSON reports, and append-only
JSONL for audit streams. Paths must be absolute and outside the repository.
Backtest, walk-forward, final-holdout, validation-summary, decision-report, and
rendered comparison outputs use the report root by default. Candidate detail,
audit, statistical-selection, and reproduction evidence remain derived
artifacts under the artifact root. Explicit output overrides are accepted only
as absolute repository-external paths validated by `ResearchPathManager`.
