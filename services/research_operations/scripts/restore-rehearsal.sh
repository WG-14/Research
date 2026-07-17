#!/bin/sh
set -eu
umask 077

backup=${1:?usage: restore-rehearsal.sh ABS_BACKUP ABS_NEW_NAMESPACE ABS_RECEIPT}
namespace=${2:?usage: restore-rehearsal.sh ABS_BACKUP ABS_NEW_NAMESPACE ABS_RECEIPT}
receipt=${3:?usage: restore-rehearsal.sh ABS_BACKUP ABS_NEW_NAMESPACE ABS_RECEIPT}
: "${POSTGRES_MAJOR:?required}"
: "${RESEARCH_OPS_RECOVERY_DATABASE_NAME:?fresh target database required}"
: "${RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH:?source identity path required}"
resume=${RESEARCH_OPS_RECOVERY_RESUME:-false}
case "$resume" in true|false) ;; *) exit 64 ;; esac
case "$namespace" in /*) ;; *) exit 64 ;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
identity_basename=$(basename -- "$RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH")
case "$identity_basename" in ''|.|..|*/*) exit 64 ;; esac

research-ops backup-verify --backup-directory "$backup" --postgresql-major "$POSTGRES_MAJOR"
table_count=$(psql -Atqc "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
test "$(psql -Atqc 'SELECT current_database()')" = "$RESEARCH_OPS_RECOVERY_DATABASE_NAME" || exit 65
if test "$resume" = false; then
  test ! -e "$namespace" || exit 73
  test "$table_count" = 0 || exit 65
  pg_restore --exit-on-error --no-owner --no-privileges --dbname "$PGDATABASE" "$backup/postgresql.dump"

  mkdir -m 0700 "$namespace"
  for role in data artifact report identity_registry; do
    mkdir -m 0700 "$namespace/$role"
    python3 "$script_dir/safe-extract.py" "$backup/$role.tar" "$namespace/$role"
  done
  mkdir -p "$namespace/data/_internal_web/manifests"
  python3 "$script_dir/safe-extract.py" "$backup/manifest.tar" \
    "$namespace/data/_internal_web/manifests"
else
  test -d "$namespace" && test "$table_count" -gt 0 || exit 66
  if test -e "$receipt" || test -e "$receipt.sig"; then
    test -f "$receipt" && test -f "$receipt.sig" || exit 73
  fi
fi
PGOPTIONS='-c default_transaction_read_only=off' psql --set=ON_ERROR_STOP=1 <<'SQL'
SELECT format(
  'ALTER DATABASE %I SET default_transaction_read_only = on',
  current_database()
) \gexec
SQL

manifest_hash=$(sha256sum "$backup/manifest.json" | awk '{print "sha256:" $1}')
marker="$namespace/.research-ops-isolated-restore-v1"
marker_value=$(printf '{"schema_version":1,"purpose":"isolated-recovery-rehearsal","backup_manifest_hash":"%s"}\n' "$manifest_hash")
if test -e "$marker"; then
  test "$(cat "$marker")" = "$marker_value" || exit 65
else
  (set -C; printf '%s\n' "$marker_value" > "$marker")
fi
export RESEARCH_OPS_RECOVERY_MODE=offline
export RESEARCH_OPS_MUTATION_DISABLED=true
export RESEARCH_DATA_ROOT="$namespace/data"
export RESEARCH_ARTIFACT_ROOT="$namespace/artifact"
export RESEARCH_REPORT_ROOT="$namespace/report"
export RESEARCH_CACHE_ROOT="$namespace/cache"
if test -e "$namespace/cache"; then
  test -d "$namespace/cache" && test ! -L "$namespace/cache" || exit 65
else
  mkdir -m 0700 "$namespace/cache"
fi
chmod 0700 "$namespace/cache"
export RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH="$namespace/identity_registry/$identity_basename"
# Recovery verification is intentionally read-only even though the isolated
# PostgreSQL server is a writable primary.
export PGOPTIONS="-c default_transaction_read_only=on -c timezone=UTC"
research-ops recovery-verify --backup-directory "$backup" \
  --restore-namespace "$namespace" --receipt-path "$receipt" \
  --postgresql-major "$POSTGRES_MAJOR"
