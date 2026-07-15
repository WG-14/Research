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

Prepare an external provenance manifest first. The checked-in
`examples/research/dataset_source_provenance.example.json` is a shape example;
replace every placeholder and recompute `provenance_manifest_hash` with the
same canonical contract before use.

Use `examples/research/sma_filter_manifest.example.json` as the structured
study shape. It separates the versioned `hypothesis_spec`, the registered
`strategy_version`, and explicit experiment conditions. Structured studies
must declare execution timing, portfolio/position sizing, and risk policy;
validation-bound studies cannot rely on legacy implicit defaults.

```bash
uv run market-research research-freeze-dataset --db /abs/candles.sqlite \
  --market KRW-BTC --interval 1m --start 2025-01-01 --end 2025-03-31 \
  --provenance-manifest /abs/dataset-source-provenance.json \
  --out /abs/datasets

uv run market-research research-readiness --manifest /abs/experiment.json --json
uv run market-research research-backtest --manifest /abs/experiment.json
uv run market-research research-walk-forward --manifest /abs/experiment.json
uv run market-research research-validate --manifest /abs/experiment.json
```

The freeze command prints the generated schema-3 `artifact_manifest_uri` and
`artifact_manifest_hash`. Put those exact values in the experiment manifest
with `dataset.source=frozen_sqlite_candles`. A mutable
`dataset.source=sqlite_candles` run is exploratory compatibility only: it is
`DECLARED_ONLY`, cannot become a validated candidate, and never receives an
authoritative reproduction receipt.

`research-validate` records research-only stages: readiness, dataset quality,
backtest, final holdout, stress suite, statistical validation, walk-forward,
final selection, and a research candidate report. Results are `PASS`, `FAIL`,
or `INSUFFICIENT_EVIDENCE`.

Automated `PASS` is not human research approval. Hypothesis and strategy
candidate lifecycle state is stored in a repository-external append-only
governance registry. Use `research-governance-transition` to record guarded
state changes and their evidence hashes, and `research-record-human-review` to
record an independent review decision. A `CHANGES_REQUESTED` review uses a JSON
array whose entries contain `requirement_id`, `description`, and
`verification_condition`; every requirement must be explicitly resolved by a
later approval.

After the hypothesis is `SUPPORTED` and the selected strategy candidate is
`OUT_OF_SAMPLE_PASSED`, create a bound approval and then export the review
package:

```bash
uv run market-research research-approve-strategy-candidate \
  --result /abs/validation-summary.json \
  --subject-version 1 \
  --reviewer reviewer-id \
  --rationale "economic rationale and overfit review passed" \
  --out /abs/strategy-approval.json

uv run market-research research-export-strategy-package \
  --result /abs/validation-summary.json \
  --approval /abs/strategy-approval.json \
  --out /abs/strategy-research-package.json
```

Strategy Research Package schema 5 is a self-contained review contract. It
includes the bound hypothesis, target market and interval, feature and rule
definitions, the complete compiled strategy contract and effective parameters,
signal/fill/exit/position/cost assumptions, permitted regimes, suspension
conditions, validation-to-holdout observed performance ranges, known
limitations, and the human approval record. Missing semantic fields fail the
export instead of producing a hash-only handoff.

The official export command verifies the schema-3 terminal result against the
canonical experiment and governance registries and emits
`authoritative=true`, `package_authority_status=CANONICAL_REGISTRIES_VERIFIED`,
and `package_authority_result=PASS`. Direct library calls without a path
manager remain a compatibility surface only: their packages are marked
`DECLARED_PATH_ONLY`/`UNVERIFIED` and are not valid approval, benchmark, or
handoff artifacts.

`validation_summary.json` schema 3 is the canonical input to both approval and
package export. It retains the complete selection contract and candidate
evidence, final-holdout confirmation, and terminal validation result in the
same logical report-hash domain verified by both commands.

`research-validate` also writes a hash-bound `research_candidate_report.json`
with fixed sections for hypothesis and conditions, data quality, performance,
trades, costs, regimes, robustness, out-of-sample evidence, failure periods,
limitations, and the automated research conclusion. It never grants
operational permission or represents automated evidence as human approval.
Render or compare those reports without rerunning the study:

```bash
uv run market-research research-render-report \
  --report /abs/reports/research/example/research_candidate_report.json \
  --out /abs/reports/research/example/research_candidate_report.md

uv run market-research research-compare \
  --report /abs/reports/research/experiment-a/research_candidate_report.json \
  --report /abs/reports/research/experiment-b/research_candidate_report.json \
  --out /abs/reports/research/comparison.json
```

Both commands verify source content hashes. Comparison is deterministic and
marks market, interval, or strategy-contract differences as compatibility
warnings.

The general transition command cannot create `RESEARCH_APPROVED`. The approval
command also checks the current hypothesis and strategy states, unresolved
review requirements, report and final-holdout hashes, strategy plugin contract,
and effective parameter hash. Retired strategies cannot reuse an old approval.

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
