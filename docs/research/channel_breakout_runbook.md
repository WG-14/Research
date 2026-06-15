# Channel Breakout Research Runbook

This runbook is for `channel_breakout_with_regime_filter` research-only PR validation.
It does not authorize paper trading, live dry-run, live real orders, approved profiles,
or capital allocation.

## Clean-Data Paired A/B Protocol

Evaluate every channel_breakout improvement as a paired control/candidate run.
The control and candidate must use the same market, interval, execution scenario,
cost model, portfolio policy, and clean subwindows. Candidate-only rows are not
valid improvement evidence.

Run readiness before backtest execution:

```bash
uv run bithumb-bot research-readiness --manifest "$MANIFEST" --json
uv run bithumb-bot research-backtest --manifest "$MANIFEST" --notification-policy require_delivery
```

Only include rows where readiness is `PASS` and the final holdout has:

```text
missing_count=0
interval_mismatch_count=0
```

Dirty periods, readiness `FAIL` periods, and degraded data windows must not be
used as robustness evidence or as improvement evidence.

The paired A/B summary must include:

```text
variant_role
period
readiness_status
final_holdout_missing_count
final_holdout_interval_mismatch_count
avg_return_pct
positive_periods
sum_trades
sum_reclaim_pnl
sum_max_hold_pnl
policy_mismatch_sum
first_entry_notional
first_entry_notional_approximately_99000
```

Reject the summary if any candidate row lacks a matching control row for the same
clean subwindow, if `policy_mismatch_sum` is missing, or if first-entry notional
verification is missing.

## Root-Cause Report

Generate a trade-level diagnostic summary before judging the candidate:

```bash
uv run python scripts/channel_breakout_rootcause.py --input "$ROOTCAUSE_INPUT_JSON"
```

The input must contain paired variant rows with `closed_trades`. The report must
include variant summary, period x variant summary, exit reason summary, holding
bucket summary, and worst/best trade samples. The holding bucket summary must
always include `00-05m` so early reclaim-loss concentration is visible.

## Acceptance Gate

Run the explicit acceptance classifier on the paired clean-data summary:

```bash
uv run python scripts/channel_breakout_acceptance.py --summary "$COMPARE_JSON"
```

Classification rules:

```text
success:
  avg_return_pct > 0
  positive_periods >= 2/3 of evaluated periods
  sum_reclaim_pnl improved versus control
  sum_max_hold_pnl not worse than control
  sum_trades did not collapse
  policy_mismatch_sum = 0
  first_entry_notional approximately 99,000

loss_reduction_only:
  avg_return_pct <= 0
  loss is reduced versus baseline
  policy_mismatch_sum = 0

fail:
  avg_return_pct <= 0
  positive_periods < 2/3 of evaluated periods
  policy_mismatch_sum > 0
  candidate sum_trades < control sum_trades * 0.25
  first_entry_notional is not approximately 99,000
```

Do not mark success from average return alone, profit factor alone, or a 1-2 trade
candidate. If `policy_mismatch_sum > 0`, stop the performance comparison and
classify the candidate as `fail`.
