# WSL research backtest runbook

Use the WSL-hosted repository from a Linux shell. Keep datasets, SQLite files,
manifests, reports, cache entries, and audit records outside the Git checkout.

```bash
uv sync
uv run market-research research-readiness --manifest /abs/experiment.json --json
uv run market-research research-backtest --manifest /abs/experiment.json
uv run market-research research-validate --manifest /abs/experiment.json
```

Set `RESEARCH_DB_PATH` to the external SQLite candle source and set the four
`RESEARCH_*_ROOT` variables when the defaults are unsuitable. Run readiness on
the exact manifest before an expensive study. Do not put generated artifacts in
the repository.

WSL may inherit Windows `TEMP` and `TMP` paths under `/mnt/c`, where Python
`forkserver` cannot bind its AF_UNIX control socket. The `scripts/platform`
test commands preflight the selected temp root and normalize `TMPDIR`, `TEMP`,
and `TMP` to `/tmp` by default. Set `RESEARCH_TEST_TMPDIR` only to another
Linux filesystem directory; direct pytest invocations must set all three temp
variables explicitly.
