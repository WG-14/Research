#!/bin/sh
set -eu
umask 077

install -d -m 0700 -o postgres -g postgres /var/lib/postgresql/tls
install -m 0600 -o postgres -g postgres /run/secrets/database_server_key /var/lib/postgresql/tls/server.key
install -m 0644 -o postgres -g postgres /run/secrets/database_server_cert /var/lib/postgresql/tls/server.crt
install -m 0644 -o postgres -g postgres /run/secrets/database_ca /var/lib/postgresql/tls/ca.crt

exec /usr/local/bin/docker-entrypoint.sh "$@"
