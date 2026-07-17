#!/bin/sh
set -eu
umask 077

# Run once as root and again after every credential or PostgreSQL policy
# change. It is deliberately idempotent and never places a password on argv.
test "$(id -u)" -eq 0 || exit 77
: "${POSTGRES_DB:?required}"
: "${POSTGRES_OWNER_USER:?required}"
: "${POSTGRES_RUNTIME_USER:?required}"
: "${POSTGRES_DIAGNOSTICS_USER:?required}"
: "${POSTGRES_VALIDATOR_USER:?required}"
: "${POSTGRES_BACKUP_USER:?required}"
: "${POSTGRES_OWNER_PASSWORD_FILE:?required}"
: "${POSTGRES_RUNTIME_PASSWORD_FILE:?required}"
: "${POSTGRES_DIAGNOSTICS_PASSWORD_FILE:?required}"
: "${POSTGRES_VALIDATOR_PASSWORD_FILE:?required}"
: "${POSTGRES_BACKUP_PASSWORD_FILE:?required}"
: "${INTERNAL_WEB_DATABASE_HOST:?required}"
: "${INTERNAL_WEB_DATABASE_SSLROOTCERT:?required}"

for identifier in "$POSTGRES_DB" "$POSTGRES_OWNER_USER" \
  "$POSTGRES_RUNTIME_USER" "$POSTGRES_DIAGNOSTICS_USER" \
  "$POSTGRES_VALIDATOR_USER" "$POSTGRES_BACKUP_USER"; do
  printf '%s\n' "$identifier" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]{0,62}$' \
    || exit 64
done
test "$POSTGRES_DB:$POSTGRES_OWNER_USER:$POSTGRES_RUNTIME_USER:$POSTGRES_DIAGNOSTICS_USER:$POSTGRES_VALIDATOR_USER:$POSTGRES_BACKUP_USER" = \
  "research:research_owner:research_runtime:research_diagnostics:research_validator:research_backup" \
  || exit 64

read_secret() {
  secret_path=$1
  case "$secret_path" in /*) ;; *) exit 64 ;; esac
  test -f "$secret_path" && test ! -L "$secret_path" || exit 65
  case "$(stat -c '%a' -- "$secret_path")" in 600|640) ;; *) exit 65 ;; esac
  secret_value=$(cat -- "$secret_path")
  test -n "$secret_value" || exit 65
  printf '%s' "$secret_value"
}

export RESEARCH_BOOTSTRAP_OWNER_PASSWORD
export RESEARCH_BOOTSTRAP_RUNTIME_PASSWORD
export RESEARCH_BOOTSTRAP_DIAGNOSTICS_PASSWORD
export RESEARCH_BOOTSTRAP_VALIDATOR_PASSWORD
export RESEARCH_BOOTSTRAP_BACKUP_PASSWORD
RESEARCH_BOOTSTRAP_OWNER_PASSWORD=$(read_secret "$POSTGRES_OWNER_PASSWORD_FILE")
RESEARCH_BOOTSTRAP_RUNTIME_PASSWORD=$(read_secret "$POSTGRES_RUNTIME_PASSWORD_FILE")
RESEARCH_BOOTSTRAP_DIAGNOSTICS_PASSWORD=$(read_secret "$POSTGRES_DIAGNOSTICS_PASSWORD_FILE")
RESEARCH_BOOTSTRAP_VALIDATOR_PASSWORD=$(read_secret "$POSTGRES_VALIDATOR_PASSWORD_FILE")
RESEARCH_BOOTSTRAP_BACKUP_PASSWORD=$(read_secret "$POSTGRES_BACKUP_PASSWORD_FILE")

native_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
postgres_dir="$native_dir/postgresql"
install -d -o root -g postgres -m 0750 /etc/research-ops/postgresql
install -o root -g postgres -m 0640 "$postgres_dir/pg_hba.conf" \
  /etc/research-ops/postgresql/pg_hba.conf
test -d /etc/postgresql/16/main/conf.d \
  && test ! -L /etc/postgresql/16/main/conf.d || exit 66
install -o root -g postgres -m 0640 \
  "$postgres_dir/90-research-operations.conf" \
  /etc/postgresql/16/main/conf.d/90-research-operations.conf

# PostgreSQL must reload the HBA location and listener/TLS postmaster settings.
systemctl restart postgresql.service

run_psql() {
  runuser --preserve-environment -u postgres -- \
    /usr/bin/psql --set=ON_ERROR_STOP=1 "$@"
}

run_psql --dbname postgres \
  --set=database="$POSTGRES_DB" \
  --set=owner_user="$POSTGRES_OWNER_USER" \
  --set=runtime_user="$POSTGRES_RUNTIME_USER" \
  --set=diagnostics_user="$POSTGRES_DIAGNOSTICS_USER" \
  --set=validator_user="$POSTGRES_VALIDATOR_USER" \
  --set=backup_user="$POSTGRES_BACKUP_USER" <<'SQL'
\getenv owner_password RESEARCH_BOOTSTRAP_OWNER_PASSWORD
\getenv runtime_password RESEARCH_BOOTSTRAP_RUNTIME_PASSWORD
\getenv diagnostics_password RESEARCH_BOOTSTRAP_DIAGNOSTICS_PASSWORD
\getenv validator_password RESEARCH_BOOTSTRAP_VALIDATOR_PASSWORD
\getenv backup_password RESEARCH_BOOTSTRAP_BACKUP_PASSWORD
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'owner_user', :'owner_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'owner_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'runtime_user', :'runtime_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'runtime_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'diagnostics_user', :'diagnostics_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'diagnostics_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'validator_user', :'validator_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'validator_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'backup_user', :'backup_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'backup_user') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', :'owner_user', :'owner_password') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', :'runtime_user', :'runtime_password') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', :'diagnostics_user', :'diagnostics_password') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', :'validator_user', :'validator_password') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', :'backup_user', :'backup_password') \gexec
SELECT format('CREATE DATABASE %I OWNER %I TEMPLATE template0 ENCODING %L', :'database', :'owner_user', 'UTF8')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'database') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'database', :'owner_user') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'database') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'database', :'owner_user') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database', role_name)
FROM (VALUES (:'runtime_user'), (:'diagnostics_user'), (:'validator_user'), (:'backup_user')) roles(role_name) \gexec
SQL

run_psql --dbname "$POSTGRES_DB" --set=owner_user="$POSTGRES_OWNER_USER" <<'SQL'
SELECT format('REVOKE CREATE ON SCHEMA public FROM PUBLIC') \gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', :'owner_user') \gexec
SQL

# Verify the active server sourced the exact native TLS/HBA policy and that
# every application role is unprivileged before any migration runs.
run_psql --dbname postgres --tuples-only --no-align \
  --set=database="$POSTGRES_DB" \
  --set=owner_user="$POSTGRES_OWNER_USER" \
  --set=runtime_user="$POSTGRES_RUNTIME_USER" \
  --set=diagnostics_user="$POSTGRES_DIAGNOSTICS_USER" \
  --set=validator_user="$POSTGRES_VALIDATOR_USER" \
  --set=backup_user="$POSTGRES_BACKUP_USER" <<'SQL' | grep -qx PASS
SELECT CASE WHEN
  current_setting('ssl') = 'on'
  AND current_setting('ssl_min_protocol_version') = 'TLSv1.2'
  AND current_setting('ssl_cert_file') = '/etc/research-ops/pki/postgres.crt'
  AND current_setting('ssl_key_file') = '/etc/research-ops/pki/postgres.key'
  AND current_setting('ssl_ca_file') = '/etc/research-ops/pki/database-ca.crt'
  AND current_setting('hba_file') = '/etc/research-ops/postgresql/pg_hba.conf'
  AND (SELECT count(*) = 5 FROM pg_roles
       WHERE rolname IN (:'owner_user', :'runtime_user', :'diagnostics_user', :'validator_user', :'backup_user')
         AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
         AND NOT rolreplication AND NOT rolbypassrls)
  AND (SELECT pg_get_userbyid(datdba) = :'owner_user'
       FROM pg_database WHERE datname = :'database')
THEN 'PASS' ELSE 'FAIL' END;
SQL

PGPASSWORD=$RESEARCH_BOOTSTRAP_RUNTIME_PASSWORD \
PGHOST=$INTERNAL_WEB_DATABASE_HOST PGPORT=5432 PGDATABASE=$POSTGRES_DB \
PGUSER=$POSTGRES_RUNTIME_USER PGSSLMODE=verify-full \
PGSSLROOTCERT=$INTERNAL_WEB_DATABASE_SSLROOTCERT \
  /usr/bin/psql --set=ON_ERROR_STOP=1 --tuples-only --no-align \
  --command="SELECT CASE WHEN ssl THEN 'PASS' ELSE 'FAIL' END FROM pg_stat_ssl WHERE pid = pg_backend_pid()" \
  | grep -qx PASS

unset RESEARCH_BOOTSTRAP_OWNER_PASSWORD RESEARCH_BOOTSTRAP_RUNTIME_PASSWORD
unset RESEARCH_BOOTSTRAP_DIAGNOSTICS_PASSWORD RESEARCH_BOOTSTRAP_VALIDATOR_PASSWORD
unset RESEARCH_BOOTSTRAP_BACKUP_PASSWORD
printf '%s\n' '{"schema_version":1,"status":"PASS","postgresql":"native-tls-scram"}'
