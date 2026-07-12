# market-research

`market-research` is an exchange-independent offline research tool that uses
externally prepared immutable market datasets to generate reproducible
backtests, walk-forward studies, statistical validation, and artifact-backed
reports. It does not provide exchange API collection, account connections,
order submission, or a runtime service.

The repository never collects market data from a network source, reads an
operational order/fill database, or performs data recovery. Supply externally
prepared immutable datasets instead. Execution calibration is an external
canonical artifact: this repository validates and consumes it, but does not
collect exchange logs or generate it from live operations. Missing candles fail
readiness; correct or replace the external dataset or SQLite input and rerun.

## Supported strategies

- `sma_with_filter`
- `buy_and_hold_baseline`
- `noop_baseline`
- `threshold_research_only`

## Install

```bash
uv sync
uv run market-research --help
```

The canonical command is:

```bash
uv run market-research <command>
```

## Research settings

All research inputs and outputs must be outside this Git repository.

- `RESEARCH_DATA_ROOT`: immutable or prepared dataset root
- `RESEARCH_ARTIFACT_ROOT`: derived traces and candidate artifacts
- `RESEARCH_REPORT_ROOT`: operator-readable research reports
- `RESEARCH_CACHE_ROOT`: disposable cache root
- `RESEARCH_DB_PATH`: SQLite candle database for commands that require it
- `RESEARCH_MAX_WORKERS`: bounded research worker count
- `RESEARCH_RANDOM_SEED`: deterministic experiment seed

Each configured path is absolute and repository-external. Research artifacts
are either atomic JSON reports or append-only JSONL audit records.

## Typical workflow

```bash
uv run market-research research-freeze-dataset --db /abs/candles.sqlite \
  --market KRW-BTC --interval 1m --start 2025-01-01 --end 2025-03-31 \
  --out /abs/datasets/krw-btc-1m.json

uv run market-research research-readiness --manifest /abs/experiment.json --json
uv run market-research research-backtest --manifest /abs/experiment.json
uv run market-research research-walk-forward --manifest /abs/experiment.json
uv run market-research research-validate --manifest /abs/experiment.json
```

`research-validate` records research-only stages: readiness, dataset quality,
backtest, final holdout, stress suite, statistical validation, walk-forward,
final selection, and a research candidate report. Results are `PASS`, `FAIL`,
or `INSUFFICIENT_EVIDENCE`.

`research-backtest` writes `reproduction_receipt.json` beside its experiment
report. Verify that result later with an isolated rerun:

```bash
uv run market-research research-reproduce-run \
  --manifest /abs/experiment.json \
  --receipt /abs/reproduction_receipt.json \
  --out /abs/reproduction_report.json
```

The receipt binds the manifest, dataset snapshot/split hashes, strategy and
execution contracts, seed scope, candidate parameters/gates, scenario behavior
and result hashes, and final selection status. It deliberately excludes
timestamps, wall time, process/memory observations, and absolute paths because
those values are not deterministic research evidence. `PASS` means this stable
evidence matches; `DRIFT` reports each differing evidence path. Reproduction
reports use these statuses (all non-`PASS` statuses exit with code `1`):

- `PASS` (exit `0`): the stable evidence matches.
- `DRIFT` (exit `1`): execution completed and stable evidence differs; the report lists each path.
- `INVALID_BASELINE` (exit `1`): the supplied receipt or its manifest/experiment binding failed preflight, so no backtest ran.
- `REPRODUCTION_FAILED` (exit `1`): the baseline was valid but isolated dataset access, execution, receipt creation, or artifact writing failed.

## Artifacts and reproducibility

Research outputs are classified under the configured roots as datasets,
derived artifacts, reports, cache entries, and append-only audit records.
The report records the manifest hash, dataset hash, parameter set, execution
assumptions, seed, and result hashes needed to reproduce a study.

## Tests

Run focused tests directly while editing a research boundary. Full-suite and
broad wrapper execution belong to the dedicated CI or pytest pipeline.
