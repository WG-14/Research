#!/bin/sh
set -eu
umask 077

runtime_password=$(tr -d '\r\n' < /run/secrets/postgres_runtime_password)
diagnostics_password=$(tr -d '\r\n' < /run/secrets/postgres_diagnostics_password)
validator_password=$(tr -d '\r\n' < /run/secrets/postgres_validator_password)
backup_password=$(tr -d '\r\n' < /run/secrets/postgres_backup_password)

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_OWNER_USER" --dbname "$POSTGRES_DB" \
  --set=runtime_user="$POSTGRES_RUNTIME_USER" \
  --set=runtime_password="$runtime_password" \
  --set=diagnostics_user="$POSTGRES_DIAGNOSTICS_USER" \
  --set=diagnostics_password="$diagnostics_password" \
  --set=validator_user="$POSTGRES_VALIDATOR_USER" \
  --set=validator_password="$validator_password" \
  --set=backup_user="$POSTGRES_BACKUP_USER" \
  --set=backup_password="$backup_password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'runtime_user', :'runtime_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'runtime_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'diagnostics_user', :'diagnostics_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'diagnostics_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'validator_user', :'validator_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'validator_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'backup_user', :'backup_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'backup_user') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'runtime_user') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'diagnostics_user') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'validator_user') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'backup_user') \gexec
SQL
