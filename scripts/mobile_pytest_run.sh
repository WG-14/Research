#!/usr/bin/env bash
set -euo pipefail

export NTFY_TOPIC=bithumb-research-dnjsckd5025

cd ~/work/bithumb-research
./scripts/run_codex_pytest_pipeline.sh
