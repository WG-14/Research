# WSL research backtest runbook

Use the WSL-hosted repository from a Linux shell. Keep datasets, SQLite files,
manifests, reports, cache entries, and audit records outside the Git checkout.

```bash
uv sync
uv run bithumb-research research-readiness --manifest /abs/experiment.json --json
uv run bithumb-research research-backtest --manifest /abs/experiment.json
uv run bithumb-research research-validate --manifest /abs/experiment.json
```

Set `RESEARCH_DB_PATH` to the external SQLite candle source and set the four
`RESEARCH_*_ROOT` variables when the defaults are unsuitable. Run readiness on
the exact manifest before an expensive study. Do not put generated artifacts in
the repository.
