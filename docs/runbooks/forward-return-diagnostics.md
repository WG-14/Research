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
- A comma-separated positive integer horizon list.
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

## Outputs

```text
DATA_ROOT/<mode>/reports/research/<experiment_id>/forward_diagnostics_report.json

DATA_ROOT/<mode>/derived/research/<experiment_id>/forward_diagnostics/feature_bucket_metrics.csv
DATA_ROOT/<mode>/derived/research/<experiment_id>/forward_diagnostics/feature_horizon_metrics.csv
DATA_ROOT/<mode>/derived/research/<experiment_id>/forward_diagnostics/warnings.json
```

The report has `artifact_type=forward_return_diagnostic_report`,
`diagnostic_only=true`, and false promotion/readiness/capital allocation
evidence flags. It also records a `calculation_policy` block containing
`entry_price_mode`, `path_start_policy`, `intrabar_included`, and
`mfe_mae_basis`; the metrics CSV outputs include the same policy columns.

## Diagnostic-only policy

forward-return diagnostics output must not be used as strategy promotion evidence
forward-return diagnostics output must not be used as approved profile evidence
forward-return diagnostics output must not be used as live readiness evidence
forward-return diagnostics output must not be used as capital allocation evidence

## Not promotion evidence

The output can suggest feature-mining hypotheses, but it does not validate a
strategy, execution model, order lifecycle, costs, risk policy, walk-forward
stability, approved profile, paper behavior, or live readiness.

## Recommended next step

If a diagnostic result suggests a useful feature, encode the hypothesis in a
research manifest and run the normal validation lifecycle with
`research-validate`.
