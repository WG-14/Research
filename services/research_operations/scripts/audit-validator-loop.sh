#!/bin/sh
set -eu

interval=${RESEARCH_OPS_AUDIT_VALIDATION_INTERVAL_SECONDS:-60}
case "$interval" in
  ''|*[!0-9]*) exit 64 ;;
esac

term=0
trap 'term=1' TERM INT
while [ "$term" -eq 0 ]; do
  research-ops audit-validate
  elapsed=0
  while [ "$term" -eq 0 ] && [ "$elapsed" -lt "$interval" ]; do
    sleep 1
    elapsed=$((elapsed + 1))
  done
done
