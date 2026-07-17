#!/bin/sh
set -eu
umask 077

: "${BACKUP_ROOT:?required}"
: "${RESEARCH_OPS_OFFSITE_RECEIPT_ROOT:?required}"
: "${RESEARCH_OPS_OFFSITE_EXPORT_HOOK:?required}"
: "${RESEARCH_OPS_OFFSITE_TARGET_ID:?required}"
: "${RESEARCH_OPS_BACKUP_ENCRYPTION:?required}"
: "${RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID:?required}"
: "${RESEARCH_OPS_BACKUP_RETENTION_DAYS:?required}"
: "${RESEARCH_OPS_BACKUP_RETENTION_MINIMUM_COUNT:?required}"
: "${RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE:?required}"
: "${RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE:?required}"
: "${RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY:=/run/research-operations}"

case "$BACKUP_ROOT:$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT:$RESEARCH_OPS_OFFSITE_EXPORT_HOOK" in
  /*:/*:/*) ;;
  *) exit 64 ;;
esac
case "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" in /*) ;; *) exit 64 ;; esac
test -d "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" \
  && test ! -L "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" || exit 65
runtime_directory=$(realpath -e -- "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY")
test "$runtime_directory" = "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" || exit 65
test "$(stat -c '%a' -- "$runtime_directory")" = 700 || exit 65
test "$(stat -c '%u' -- "$runtime_directory")" = "$(id -u)" || exit 65

native_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
service_root=$(CDPATH= cd -- "$native_dir/../../.." && pwd)
output=$(mktemp "$runtime_directory/backup-output.XXXXXX")
trap 'rm -f "$output"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$service_root/scripts/create-backup.sh" >"$output"
backup=$(tail -n 1 "$output")
case "$backup" in "$BACKUP_ROOT"/*) ;; *) exit 65 ;; esac
test -d "$backup" && test ! -L "$backup" || exit 65
backup=$(realpath -e -- "$backup")
test "$(dirname -- "$backup")" = "$(realpath -e -- "$BACKUP_ROOT")" || exit 65
backup_id=$(basename -- "$backup")
receipt="$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT/$backup_id.json"
test ! -e "$receipt" && test ! -L "$receipt" || exit 73

"$RESEARCH_OPS_OFFSITE_EXPORT_HOOK" export \
  --backup-directory "$backup" \
  --target-id "$RESEARCH_OPS_OFFSITE_TARGET_ID" \
  --encryption "$RESEARCH_OPS_BACKUP_ENCRYPTION" \
  --encryption-key-id "$RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID" \
  --receipt "$receipt"

python3 "$native_dir/verify-offsite-receipt.py" \
  --receipt "$receipt" --backup-directory "$backup" --backup-id "$backup_id" \
  --target-id "$RESEARCH_OPS_OFFSITE_TARGET_ID" \
  --encryption "$RESEARCH_OPS_BACKUP_ENCRYPTION" \
  --encryption-key-id "$RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID" \
  --verification-public-key "$RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE"

python3 "$native_dir/backup-retention.py" --dry-run \
  --backup-root "$BACKUP_ROOT" \
  --receipt-root "$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT" \
  --backup-verification-public-key "$RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE" \
  --offsite-receipt-verification-public-key \
    "$RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE" \
  --target-id "$RESEARCH_OPS_OFFSITE_TARGET_ID" \
  --encryption "$RESEARCH_OPS_BACKUP_ENCRYPTION" \
  --encryption-key-id "$RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID" \
  --retention-days "$RESEARCH_OPS_BACKUP_RETENTION_DAYS" \
  --minimum-count "$RESEARCH_OPS_BACKUP_RETENTION_MINIMUM_COUNT"
