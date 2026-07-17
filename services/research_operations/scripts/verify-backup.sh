#!/bin/sh
set -eu
: "${1:?usage: verify-backup.sh ABSOLUTE_BACKUP_DIRECTORY}"
: "${POSTGRES_MAJOR:?PostgreSQL major required}"
research-ops backup-verify --backup-directory "$1" --postgresql-major "$POSTGRES_MAJOR"
