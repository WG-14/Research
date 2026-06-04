# Pre-Merge Validation Checklist

Run these repository-local checks before merging changes that touch config,
operator output, docs, templates, live safety, or runtime contracts.
The `safety-regression` GitHub Actions workflow runs the same targeted gate
commands after `uv sync --dev` and virtualenv activation.

```bash
python3 tools/check_text_hygiene.py
python3 tools/check_env_drift.py
python3 tools/generate_config_docs.py --check
python3 tools/generate_env_example.py --check
python3 -m pytest tests/test_text_hygiene.py tests/test_config_contract.py -q
python3 -m pytest tests/test_live_preflight.py::test_live_execution_contract_emits_safe_env_metadata_and_lints tests/test_live_preflight.py::test_live_execution_contract_log_emits_redacted_fingerprint -q
python3 -m pytest tests/test_operator_commands.py::test_cmd_signal_no_data_output_is_clean_and_actionable tests/test_operator_commands.py::test_cmd_explain_no_data_output_is_clean_and_actionable tests/test_operator_commands.py::test_cmd_status_missing_candle_output_is_clean_and_actionable -q
```

The default PR fast-suite gate is:

```bash
./scripts/run_fast_pr_tests.sh
```

It runs the static research runner marker/inventory policy check and then runs
pytest excluding `research_kernel`, `research_e2e`, `audit_e2e`,
`walk_forward_e2e`, `parallel_e2e`, `nightly`, `slow_research`, and
`memory_sensitive`, with duration reporting enabled. The fast script also parses
the reported durations and fails default-fast tests over the configured fast
threshold. The policy check prints suite-level expensive research workload
totals, including strategy count, manifest count, strategy canary count,
estimated strategy runs, estimated tick events, and estimated audit stream rows.

The dedicated research/nightly pytest suite is:

```bash
./scripts/run_research_nightly_tests.sh
```

This fast suite must not include full research matrices, complete-external audit
research runs, walk-forward E2E, serial/parallel real research comparisons, or
memory-sensitive checks. It must also avoid production research evaluators and
unbounded strategy/kernel tick loops; direct kernel tests in the fast suite must
stay bounded in-memory micro-kernel contracts. Run research E2E/nightly
validation through `scripts/run_research_nightly_tests.sh`, which includes
`research_kernel`, `research_e2e`, `audit_e2e`, `walk_forward_e2e`,
`parallel_e2e`, `nightly`, `slow_research`, and `memory_sensitive`, then checks
their durations against `tests/policy/research_e2e_inventory.json`.

Selector-less full pytest is long-running/full validation and is not the
default PR check. Use the dedicated pytest pipeline for full-suite repair or
final full validation when required.

`scripts/run_codex_pytest_pipeline.sh` is Codex full-pytest repair automation
that may commit, push, and perform EC2 smoke verification. It is not the
dedicated research/nightly pytest suite.

Required gate coverage:

- Text hygiene rejects BOM, Hangul, replacement characters, long question runs,
  and known mojibake fragments.
- Env drift rejects undeclared runtime env reads, undeclared `.env.example`
  keys, unverified docs/example drift, unsafe secret examples, unlabeled
  deprecated keys, and missing live-required examples.
- Config reference and `.env.example` stay verified against ConfigSpec.
- Live execution contract metadata includes config, docs, template, effective
  settings, env-file, provenance, approved-profile, managed-root, and runtime
  path fingerprints.
- Bithumb JWT auth warning budget is zero: `jwt.exceptions.InsecureKeyLengthWarning`
  is a test failure, and live-like tests must use centralized HS256-safe Bithumb
  test auth material.
- Operator-facing no-data diagnostics stay English, reason-coded, and
  action-oriented.
