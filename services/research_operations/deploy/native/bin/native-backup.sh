#!/bin/sh
set -eu
umask 027

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
: "${RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY:=/run/research-operations-backup}"

case "$BACKUP_ROOT:$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT:$RESEARCH_OPS_OFFSITE_EXPORT_HOOK" in
  /*:/*:/*) ;;
  *) exit 64 ;;
esac
case "$RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY" in /*) ;; *) exit 64 ;; esac
test -d "$BACKUP_ROOT" && test ! -L "$BACKUP_ROOT" || exit 65
test -d "$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT" \
  && test ! -L "$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT" || exit 65
backup_root=$(realpath -e -- "$BACKUP_ROOT")
receipt_root=$(realpath -e -- "$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT")
test "$backup_root" = "$BACKUP_ROOT" || exit 65
test "$receipt_root" = "$RESEARCH_OPS_OFFSITE_RECEIPT_ROOT" || exit 65
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

validate_receipt_file() {
  candidate=$1
  test -f "$candidate" && test ! -L "$candidate" || exit 65
  test "$(stat -c '%a' -- "$candidate")" = 640 || exit 65
  test "$(stat -c '%u' -- "$candidate")" = "$(id -u)" || exit 65
  test "$(stat -c '%g' -- "$candidate")" = "$(id -g)" || exit 65
}

verify_receipt() {
  receipt_path=$1
  backup_path=$2
  staging=$3
  if test "$staging" = true; then
    staging_argument=--allow-staging-directory
  else
    staging_argument=
  fi
  python3 "$native_dir/verify-offsite-receipt.py" \
    --receipt "$receipt_path" --backup-directory "$backup_path" \
    --backup-id "$backup_id" --target-id "$RESEARCH_OPS_OFFSITE_TARGET_ID" \
    --encryption "$RESEARCH_OPS_BACKUP_ENCRYPTION" \
    --encryption-key-id "$RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID" \
    --verification-public-key \
      "$RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE" \
    $staging_argument
}

BACKUP_DEFER_FINALIZATION=true \
  "$service_root/scripts/create-backup.sh" >"$output"
backup=$(tail -n 1 "$output")
case "$backup" in "$BACKUP_ROOT"/*) ;; *) exit 65 ;; esac
test -d "$backup" && test ! -L "$backup" || exit 65
backup=$(realpath -e -- "$backup")
test "$(dirname -- "$backup")" = "$backup_root" || exit 65
backup_name=$(basename -- "$backup")
case "$backup_name" in
  .staging-*)
    backup_id=${backup_name#\.staging-}
    backup_is_staging=true
    ;;
  *)
    backup_id=$backup_name
    backup_is_staging=false
    ;;
esac
printf '%s\n' "$backup_id" | grep -Eq \
  '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' \
  || exit 65
receipt="$receipt_root/$backup_id.json"

if test "$backup_is_staging" = false; then
  # A finalized directory is reachable only after a prior verified receipt
  # publication.  Never start a new upload while a UUID final is visible.
  validate_receipt_file "$receipt"
  verify_receipt "$receipt" "$backup" false
else
  if test -e "$receipt" || test -L "$receipt"; then
    # Resume the narrow crash window after receipt publication but before the
    # local directory rename.  The existing final receipt must verify exactly.
    validate_receipt_file "$receipt"
    verify_receipt "$receipt" "$backup" true
  else
    attempt_id=$(cat /proc/sys/kernel/random/uuid)
    printf '%s\n' "$attempt_id" | grep -Eq \
      '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' \
      || exit 65
    receipt_attempt="$receipt_root/.staging-$backup_id-$attempt_id.json"
    test ! -e "$receipt_attempt" && test ! -L "$receipt_attempt" || exit 73

    "$RESEARCH_OPS_OFFSITE_EXPORT_HOOK" export \
      --backup-directory "$backup" \
      --target-id "$RESEARCH_OPS_OFFSITE_TARGET_ID" \
      --encryption "$RESEARCH_OPS_BACKUP_ENCRYPTION" \
      --encryption-key-id "$RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID" \
      --receipt "$receipt_attempt"

    validate_receipt_file "$receipt_attempt"
    verify_receipt "$receipt_attempt" "$backup" true
    # Hard-link publication is same-filesystem and no-replace.  A crash after
    # this point leaves a verified final receipt and a still-hidden local set;
    # the next exact BACKUP_RESUME_ID run verifies both and completes the rename.
    ln -- "$receipt_attempt" "$receipt"
    sync -f "$receipt_root"
    rm -f -- "$receipt_attempt"
    sync -f "$receipt_root"
  fi

  final="$backup_root/$backup_id"
  test ! -e "$final" && test ! -L "$final" || exit 73
  mv -n -T -- "$backup" "$final"
  test ! -e "$backup" && test ! -L "$backup" || exit 73
  test -d "$final" && test ! -L "$final" || exit 65
  sync -f "$backup_root"
  backup=$final
  validate_receipt_file "$receipt"
  verify_receipt "$receipt" "$backup" false
fi

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
printf '%s\n' "$backup"
