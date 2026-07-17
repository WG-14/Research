#!/bin/sh
set -eu
umask 077

: "${BACKUP_ROOT:?absolute backup root required}"
: "${BACKUP_OPERATOR_ID:?operator id required}"
: "${POSTGRES_MAJOR:?PostgreSQL major required}"
: "${RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY:=/run/research-operations}"
case "$BACKUP_ROOT" in /*) ;; *) exit 64 ;; esac
test -d "$BACKUP_ROOT" || exit 66
case "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" in /*) ;; *) exit 64 ;; esac
test -d "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" \
  && test ! -L "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" || exit 65
runtime_directory=$(realpath -e -- "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY")
test "$runtime_directory" = "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" || exit 65
test "$(stat -c '%a' -- "$runtime_directory")" = 700 || exit 65
test "$(stat -c '%u' -- "$runtime_directory")" = "$(id -u)" || exit 65

resume_id=${BACKUP_RESUME_ID:-}
if test -n "$resume_id"; then
  printf '%s\n' "$resume_id" | grep -Eq \
    '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || exit 64
  backup_id=$resume_id
else
  backup_id=$(cat /proc/sys/kernel/random/uuid)
fi
staging="$BACKUP_ROOT/.staging-$backup_id"
final="$BACKUP_ROOT/$backup_id"
receipt="$runtime_directory/backup-fence-$backup_id.json"
sealed=0
if test -n "$resume_id"; then
  test -d "$staging" && test ! -e "$final" && test -f "$receipt" || exit 66
  research-ops backup-fence reconcile --receipt "$receipt" >/dev/null
  phase=$(research-ops backup-fence status | jq -er '.phase')
  case "$phase" in
    DRAINING) ;;
    SEALED) sealed=1 ;;
    *) exit 65 ;;
  esac
else
  test ! -e "$staging" && test ! -e "$final" && test ! -e "$receipt" || exit 73
  mkdir -m 0700 "$staging"
  research-ops backup-fence begin --operator-id "$BACKUP_OPERATOR_ID" \
    --reason scheduled_coherent_backup --receipt "$receipt"
fi

if test "$sealed" -eq 0; then
  # Mutations are now drained, but delivery claims remain open. Wait for workers.
  attempt=0
  while :; do
    status=$(research-ops backup-fence status)
    if printf '%s' "$status" | jq -e '.phase == "DRAINING" and (.counts | to_entries | all(.value == 0))' >/dev/null; then
      break
    fi
    attempt=$((attempt + 1))
    test "$attempt" -lt 120 || exit 75
    sleep 2
  done
  research-ops audit-validate
  research-ops backup-fence seal --receipt "$receipt" --audit-max-age-seconds 300
fi

pg_dump --format=custom --serializable-deferrable --no-owner --no-privileges \
  --file "$staging/postgresql.dump"
sync -f "$staging/postgresql.dump"

archive_root() {
  role=$1
  root=$2
  test -d "$root" || exit 66
  if find "$root" -type l -print -quit | grep -q .; then exit 65; fi
  if test "$role" = data; then
    # The writable manifest subtree is a separate signed backup role. Exclude
    # it here so restore never overlays two archives onto the same paths.
    tar --create --file "$staging/$role.tar" --one-file-system --numeric-owner \
      --acls --xattrs --exclude='./_internal_web/manifests' \
      --directory "$root" .
  else
    tar --create --file "$staging/$role.tar" --one-file-system --numeric-owner \
      --acls --xattrs --directory "$root" .
  fi
  sync -f "$staging/$role.tar"
}
archive_root data "$RESEARCH_DATA_ROOT"
archive_root manifest "$RESEARCH_DATA_ROOT/_internal_web/manifests"
archive_root artifact "$RESEARCH_ARTIFACT_ROOT"
archive_root report "$RESEARCH_REPORT_ROOT"
archive_root identity_registry "$(dirname "$RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH")"

research-ops backup-manifest-create \
  --backup-directory "$staging" --fence-receipt "$receipt" \
  --postgresql-major "$POSTGRES_MAJOR" --backup-id "$backup_id" \
  --file postgresql=postgresql.dump --file data=data.tar \
  --file manifest=manifest.tar \
  --file artifact=artifact.tar --file report=report.tar \
  --file identity_registry=identity_registry.tar > "$staging/verification.json"
manifest_hash=$(jq -er '.manifest_hash' "$staging/verification.json")
mv "$staging" "$final"
sync -f "$BACKUP_ROOT"
research-ops backup-fence reopen --receipt "$receipt" \
  --manifest-hash "$manifest_hash" --operator-id "$BACKUP_OPERATOR_ID"
printf '%s\n' "$final"
