# WSL Research Backtest Runbook

## GPT Quick Context

Use this document when answering WSL or Linux questions about running a backtest, `research-backtest`, `research-validate`, or `research-readiness` for this repository. WSL/Linux is the local reference behavior for execution, paths, locking, process behavior, and operational validation. Use `uv run bithumb-bot ...` as the canonical CLI form. Use `BITHUMB_ENV_FILE` with repo-external runtime roots such as `DATA_ROOT`; keep any `DB_PATH` repo-external too. Do not recommend `python backtest.py` as the official backtest path. Root `backtest.py` is a fail-closed diagnostic smoke wrapper only. Use `research-readiness` before expensive research runs. Use `research-validate --manifest <path>` as the normal validation path. Use `research-backtest --manifest <path>` only for diagnostic/development investigation unless the full validation lifecycle binds the evidence. Smoke output is not promotion-grade. Runtime artifacts, research outputs, pytest workspaces, reports, derived artifacts, traces, logs, and DB files must stay outside the Git repository.

## Scope

This runbook covers local WSL execution for:

- Research readiness checks.
- Manifest-backed research validation.
- Diagnostic research backtests.
- Locating generated research artifacts.
- Interpreting common failure boundaries.
- Avoiding repository-local runtime artifacts.

This runbook does not authorize strategy promotion by itself, approved profile generation by itself, paper trading approval, live dry-run approval, live real-order approval, or capital allocation.

## Source of Truth

Follow these documents first:

- `AGENTS.md`
- `docs/storage-layout.md`
- `docs/runtime-data-policy.md`
- `docs/research-validation.md`
- `docs/runbooks/research-to-paper.md`

This WSL runbook is an execution guide. It must not weaken the research validation lifecycle or its evidence requirements.

## WSL Assumptions

Clone the repository inside the WSL filesystem, not under `/mnt/c/...`.

Open the WSL-hosted repository with VS Code Remote WSL, run commands from a WSL shell, and treat Linux path behavior as the local source of truth. Native Windows execution may be convenient for editing, but it is not evidence for runtime correctness.

## Command Classification

| Command | Use | Evidence boundary |
| --- | --- | --- |
| `uv run bithumb-bot research-readiness --manifest <path>` | Preflight for manifest data, DB, split, top-of-book, calibration, walk-forward prerequisites | Readiness only |
| `uv run bithumb-bot research-validate --manifest <path>` | Normal validation lifecycle | Official validation path when required stages pass |
| `uv run bithumb-bot research-backtest --manifest <path>` | Diagnostic/development investigation | Not promotion-grade by itself |
| `uv run bithumb-bot research-walk-forward --manifest <path>` | Direct diagnostic walk-forward investigation | Usually run by `research-validate` when required |
| `python backtest.py` | Do not use as official path | Fail-closed smoke wrapper |
| `python backtest.py --diagnostic-smoke-only` | Explicit smoke check only | Non-promotable smoke output |

## One-Time Setup

```bash
uv sync
uv run bithumb-bot health
```

Canonical CLI form:

```bash
uv run bithumb-bot <command>
```

Use CLI commands so the bootstrap and explicit env loading path is exercised. Raw ad-hoc Python imports are not the supported path for runtime config validation.

## Runtime Roots and Env File

Use explicit WSL repo-external runtime roots:

```bash
BITHUMB_WSL_ROOT="$HOME/.local/state/bithumb-bot-wsl"
mkdir -p "$BITHUMB_WSL_ROOT"/{env,run,data,logs,backup,archive}

cat > "$BITHUMB_WSL_ROOT/env/paper.research.env" <<EOF
MODE=paper
ENV_ROOT=$BITHUMB_WSL_ROOT/env
RUN_ROOT=$BITHUMB_WSL_ROOT/run
DATA_ROOT=$BITHUMB_WSL_ROOT/data
LOG_ROOT=$BITHUMB_WSL_ROOT/logs
BACKUP_ROOT=$BITHUMB_WSL_ROOT/backup
ARCHIVE_ROOT=$BITHUMB_WSL_ROOT/archive
MARKET=KRW-BTC
INTERVAL=1m
STRATEGY_NAME=sma_with_filter
RESEARCH_NOTIFICATION_POLICY=disabled
EOF
```

Inspect the masked configuration through the CLI:

```bash
BITHUMB_ENV_FILE="$BITHUMB_WSL_ROOT/env/paper.research.env" \
uv run bithumb-bot config-dump --masked
```

Do not put `DATA_ROOT`, `DB_PATH`, reports, derived artifacts, traces, or logs inside the Git repository.

In paper mode, unset roots may fall back under `XDG_STATE_HOME/bithumb-bot` or `~/.local/state/bithumb-bot`, but this runbook uses explicit repo-external roots to avoid ambiguity.

## Manifest Selection

Repository example manifest:

```bash
MANIFEST="examples/research/sma_filter_manifest.example.json"
```

Operator research should prefer a repository-external manifest path:

```bash
MANIFEST="$DATA_ROOT/paper/reports/research/manifests/<experiment>.json"
```

If you use `$DATA_ROOT` in shell snippets, set it to the same repo-external value used in the env file, for example:

```bash
DATA_ROOT="$BITHUMB_WSL_ROOT/data"
```

The manifest should define the hypothesis, dataset split dates, snapshot id, parameter space, cost model, execution model, acceptance gate, and walk-forward configuration. Do not tune runtime env values until a backtest looks good.

## Preflight: Config and Readiness

Inspect config first:

```bash
BITHUMB_ENV_FILE="$BITHUMB_WSL_ROOT/env/paper.research.env" \
uv run bithumb-bot config-dump --masked
```

Run readiness before expensive research:

```bash
BITHUMB_ENV_FILE="$BITHUMB_WSL_ROOT/env/paper.research.env" \
uv run bithumb-bot research-readiness --manifest "$MANIFEST"
```

Optional JSON output to a repo-external path:

```bash
mkdir -p "$BITHUMB_WSL_ROOT/data/paper/reports/research"

BITHUMB_ENV_FILE="$BITHUMB_WSL_ROOT/env/paper.research.env" \
uv run bithumb-bot research-readiness --manifest "$MANIFEST" --json \
  | tee "$BITHUMB_WSL_ROOT/data/paper/reports/research/readiness.preview.json"
```

Inspect:

- `status`
- `manifest_path`
- `manifest_hash`
- `mode`
- `db_path`
- `env_file`
- `env_loaded`
- `env_exists`
- `market`
- `interval`
- `splits`
- `top_of_book`
- `execution_capability`
- `execution_calibration`
- `walk_forward`
- `next_actions`

## Official Validation Path

Use `research-validate` for the normal validation lifecycle:

```bash
BITHUMB_ENV_FILE="$BITHUMB_WSL_ROOT/env/paper.research.env" \
uv run bithumb-bot research-validate --manifest "$MANIFEST"
```

With execution calibration:

```bash
BITHUMB_ENV_FILE="$BITHUMB_WSL_ROOT/env/paper.research.env" \
uv run bithumb-bot research-validate \
  --manifest "$MANIFEST" \
  --execution-calibration "$DATA_ROOT/paper/reports/execution_quality/<calibration>.json"
```

`research-validate` is the normal validation lifecycle command. It can run readiness, backtest, policy-required walk-forward, promotion, reproduce, and write `validation_run.json`.

Validation stages include:

- readiness
- dataset_quality
- backtest
- final_holdout
- stress_suite
- statistical_validation
- final_selection
- walk_forward
- promotion_eligibility
- promotion
- reproduce

## Diagnostic Backtest Path

For diagnostic/development investigation:

```bash
BITHUMB_ENV_FILE="$BITHUMB_WSL_ROOT/env/paper.research.env" \
uv run bithumb-bot research-backtest --manifest "$MANIFEST"
```

A successful `research-backtest` process exit means the diagnostic command completed. It does not mean the strategy is promotion-ready, paper-ready, live-ready, or capital-allocation-ready.

If walk-forward is required, a standalone report may correctly show `standalone_backtest_not_full_validation=true` or `walk_forward_required_but_not_executed_in_this_run`. In that case, run `research-validate` for the full lifecycle.

## Smoke Backtest Boundary

Do not use this as the research validation path:

```bash
python backtest.py
```

Explicit smoke-only execution is:

```bash
python backtest.py --diagnostic-smoke-only
```

Root `backtest.py` is a fail-closed diagnostic smoke wrapper only. Smoke output is non-promotable and must not be used for strategy promotion, approved profiles, live readiness, or capital allocation.

## Artifact Locations

Research outputs belong under managed runtime roots, not under the Git repository:

```text
DATA_ROOT/<mode>/reports/research/<experiment_id>/
DATA_ROOT/<mode>/derived/research/<experiment_id>/
DATA_ROOT/<mode>/reports/research/<experiment_id>/validation_run.json
DATA_ROOT/<mode>/reports/research/<experiment_id>/backtest_report.json
DATA_ROOT/<mode>/reports/research/<experiment_id>/walk_forward_report.json
DATA_ROOT/<mode>/reports/research/<experiment_id>/promotion_<candidate_id>.json
```

Reports are operator-readable runtime artifacts. Derived research outputs are computed intermediates. Keep both under repo-external `DATA_ROOT`.

## Report Inspection Commands

Backtest report inspection:

```bash
REPORT="$DATA_ROOT/paper/reports/research/<experiment_id>/backtest_report.json"

jq '{
  manifest_hash,
  dataset_content_hash,
  dataset_quality_hash,
  dataset_quality_gate_status,
  dataset_quality_gate_reasons,
  content_hash,
  best_candidate_id,
  promotion_eligibility_gate_result,
  promotion_blocking_reasons,
  promotion_allowed,
  next_action
}' "$REPORT"
```

Validation run inspection:

```bash
VALIDATION_RUN="$DATA_ROOT/paper/reports/research/<experiment_id>/validation_run.json"

jq '{
  validation_run_id,
  experiment_id,
  manifest_hash,
  validation_policy_source,
  validation_policy_required_stage_names,
  required_stage_names,
  selected_candidate_id,
  backtest_report_hash,
  walk_forward_report_hash,
  promotion_artifact_hash,
  reproduce_ok,
  promotion_allowed,
  end_to_end_validation_result,
  fail_closed_reasons
}' "$VALIDATION_RUN"

jq '.stages[] | {
  name,
  required,
  status,
  reasons,
  artifact_paths,
  artifact_hashes
}' "$VALIDATION_RUN"
```

## Parallel Research on WSL

`PYTEST_XDIST_WORKERS` does not control research CLI workers.

Configure research execution in the manifest:

```json
"research_run": {
  "execution": {
    "mode": "parallel",
    "max_workers": 8,
    "process_start_method": "auto_safe",
    "work_unit": "candidate_scenario"
  }
}
```

Optional caps:

```bash
export BITHUMB_RESEARCH_MAX_WORKERS=4
export BITHUMB_TOTAL_PROCESS_BUDGET=6
```

Effective workers may be lower than requested. Inspect reports for execution observability.

## Disk and Workspace Safety

Before runs:

```bash
df -h /
du -sh "$BITHUMB_WSL_ROOT" /tmp/bithumb-bot-pytest-* /tmp/pytest-of-$USER 2>/dev/null || true
./scripts/check_repo_runtime_artifacts.sh
```

After runs:

```bash
./scripts/check_repo_runtime_artifacts.sh
df -h /
du -sh "$BITHUMB_WSL_ROOT" /tmp/bithumb-bot-pytest-* /tmp/pytest-of-$USER 2>/dev/null || true
```

Do not clean up by deleting random files inside the Git repository. Generated runtime and research artifacts should not be there in the first place.

## Failure Interpretation

| Symptom | Meaning | Next action |
| --- | --- | --- |
| `python backtest.py` exits 2 | Expected fail-closed smoke wrapper behavior | Use `research-validate --manifest <path>` |
| `research-readiness` fails | Dataset/env/calibration/walk-forward prerequisite is not ready | Inspect `next_actions`; fix data/env/manifest first |
| `dataset_quality_gate_status=FAIL` | Dataset evidence failure | Fix dataset or manifest; do not tune strategy around it |
| `walk_forward_required_but_not_executed_in_this_run` | Standalone diagnostic backtest did not run full lifecycle | Run `research-validate` |
| `promotion_allowed=0` | Candidate is not promotable | Do not run profile generation or live readiness from this evidence |
| `validation_run_not_passed` | Full validation did not pass | Inspect `.stages[]` in `validation_run.json` |
| repo artifact checker fails | Runtime/research artifacts leaked into repo | Move outputs to managed runtime roots and fix path usage |

## Do Not Do

- Do not run `python backtest.py` as the official research path.
- Do not treat smoke output as promotion evidence.
- Do not treat standalone `research-backtest` success as paper/live readiness.
- Do not write `DATA_ROOT`, `DB_PATH`, reports, derived artifacts, traces, or logs into the repository.
- Do not use `./data`, `./tmp`, `./backups`, or repo-root `*.log` for runtime artifacts.
- Do not edit generated report hashes, registry rows, validation runs, or promotion artifacts by hand.
- Do not tune runtime env values until a backtest looks good.
- Do not use native Windows path behavior as runtime correctness evidence.

## Related Documents

- `AGENTS.md`
- `README.md`
- `docs/storage-layout.md`
- `docs/runtime-data-policy.md`
- `docs/research-validation.md`
- `docs/runbooks/research-to-paper.md`
- `docs/pre-merge-checklist.md`
