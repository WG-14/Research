# Forward Return Diagnostics

## Purpose

Use `research-forward-diagnostics` to inspect as-of feature buckets against
future gross returns, MFE, and MAE for feature-mining diagnostics. The command is
read-only with respect to trading state and is outside strategy promotion,
approved profile, runtime replay, and live execution boundaries.

## Inputs

- A research manifest.
- A manifest split: `train` or `validation` for normal feature-mining diagnostics.
  `final_holdout` requires the explicit `--allow-final-holdout-diagnostics`
  override and records contamination-risk metadata in the report.
- A comma-separated feature list.
- A comma-separated positive integer horizon list. Horizon values are candle
  steps, not wall-clock duration strings; `--horizons 5` means five candles.
- A bucket method such as `quantile:10`.
- An entry price mode: `next_open` or `signal_close`.

## Entry price and MFE/MAE policy

Use `next_open` when the diagnostic should be closest to an executable
next-candle assumption. In this mode, the entry price is the next candle open
and the MFE/MAE path starts with that entry candle, so the entry candle intrabar
high and low are included in the path calculation.

`signal_close` is a diagnostic convenience only. It uses the signal candle close
as the entry price, but OHLC candles cannot reveal post-close intrabar movement
inside the already-closed signal candle. For `signal_close`, MFE/MAE therefore
starts from the next candle after the signal close:

```text
entry_price_mode=signal_close
path_start_policy=next_candle_after_signal_close
intrabar_included=false
mfe_mae_basis=ohlc_future_candles_only
```

Do not read `signal_close` output as fill simulation, order lifecycle evidence,
or proof that the signal candle high or low was reachable after the close.
Prefer `next_open` for diagnostics that may later inform an execution-model
hypothesis.

## Command

```bash
uv run bithumb-bot research-forward-diagnostics \
  --manifest <manifest.json> \
  --split train \
  --features sma_gap,range_ratio,volume_ratio,breakout_distance,rolling_return,zscore,regime \
  --horizons 1,3,5 \
  --bucket quantile:10 \
  --entry-price next_open \
  --min-bucket-count 30 \
  --allow-degraded-diagnostics \
  --json
```

`final_holdout` is not the normal feature-mining path. To inspect it anyway,
operators must pass:

```bash
--split final_holdout --allow-final-holdout-diagnostics
```

Reports produced with that override record
`final_holdout_diagnostic_override=true` and include the machine-readable warning
`final_holdout_diagnostic_contamination_risk`.

Registry accounting is not used for this diagnostic override. The experiment
registry is reserved for research-validation and promotion custody accounting;
forward-return diagnostics remain report-only policy evidence through the
override flag and contamination-risk warning.

## Outputs

```text
DATA_ROOT/<mode>/reports/research/<experiment_id>/forward_diagnostics_report.json

DATA_ROOT/<mode>/derived/research/<experiment_id>/forward_diagnostics/feature_bucket_metrics.csv
DATA_ROOT/<mode>/derived/research/<experiment_id>/forward_diagnostics/feature_horizon_metrics.csv
DATA_ROOT/<mode>/derived/research/<experiment_id>/forward_diagnostics/warnings.json
```

The report has `artifact_type=forward_return_diagnostic_report`,
`diagnostic_only=true`, and false promotion/readiness/capital allocation
evidence flags. It also records `evidence_scope=diagnostic_feature_mining`,
`promotion_eligible=false`, `promotion_grade=false`, `non_promotable=true`,
machine-readable `forbidden_uses`, and `operator_next_action`. It records a
`calculation_policy` block containing `entry_price_mode`, `path_start_policy`,
`intrabar_included`, and `mfe_mae_basis`; the metrics CSV outputs include the
same policy columns.

The report and warnings artifact also record this first-class measurement
contract:

```json
{
  "return_basis": "gross_forward_return",
  "cost_adjustment": "none",
  "diagnostic_cost_model": "none",
  "execution_simulation": false,
  "fill_simulation": false,
  "order_lifecycle_simulation": false,
  "operator_interpretation": "feature_mining_only_not_expected_pnl"
}
```

`calculation_policy` only describes entry price, path start, intrabar handling,
and MFE/MAE basis. Return basis, costs, fills, execution simulation, and order
lifecycle semantics belong to `measurement_contract`.

Both metric CSV files include `return_basis=gross_forward_return`,
`cost_adjustment=none`, `execution_simulation=false`, `fill_simulation=false`,
`sample_start_ts`, `sample_end_ts`, and gross-return columns such as
`mean_gross_forward_return`. The sample time range is based on forward target
`entry_ts`, the timestamp where the gross forward-return measurement starts.
Compatibility alias columns such as `mean_forward_return` may exist, but they
are derived from the gross-return fields and are not cost-adjusted returns.

Horizon labels in metric CSV outputs remain candle-step labels such as `5c`.
The report also records `interval` and `horizon_durations`; for example,
`horizon_steps=5` on `interval=5m` records `horizon_label=5c` and
`horizon_duration_label=25m`.

`feature_bucket_metrics.csv` contains quantile or category bucket rows. By
contrast, `feature_horizon_metrics.csv` contains one aggregate row per
feature/horizon and does not contain `bucket_id` or `bucket_label`.

Categorical feature provider specs may declare a `category_universe`. Declared
but unobserved categories appear as zero-count category buckets, and observed
values outside the declared universe emit machine-readable category drift
warnings such as `unknown_category_value`.

## Diagnostic-only policy

forward-return diagnostics output must not be used as strategy promotion evidence
forward-return diagnostics output must not be used as approved profile evidence
forward-return diagnostics output must not be used as live readiness evidence
forward-return diagnostics output must not be used as capital allocation evidence

## Degraded diagnostics

`diagnostic_status=available` exits with code `0`.
`diagnostic_status=degraded` still writes the diagnostic report, but exits with
code `1` unless `--allow-degraded-diagnostics` is passed. With that explicit
override, degraded diagnostics exit with code `0`. The report records
`degraded_override` and `degraded_exit_policy` so automation can distinguish
unqualified success from an operator-accepted degraded diagnostic. Unavailable
diagnostics write a failure artifact and exit with code `1` regardless of the
degraded override.

## Not promotion evidence

The output can suggest feature-mining hypotheses, but it does not validate a
strategy, execution model, order lifecycle, costs, risk policy, walk-forward
stability, approved profile, paper behavior, or live readiness.

## Recommended next step

If a diagnostic result suggests a useful feature, encode the hypothesis in a
research manifest and run the normal validation lifecycle with
`research-validate`.
