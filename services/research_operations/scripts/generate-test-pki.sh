#!/bin/sh
set -eu
umask 077

output=${1:?usage: generate-test-pki.sh ABSOLUTE_NEW_DIRECTORY [proxy-dns]}
proxy_dns=${2:-research.internal.example}
case "$output" in /*) ;; *) exit 64 ;; esac
test ! -e "$output" || exit 73
mkdir -m 0700 "$output"

openssl req -x509 -newkey rsa:3072 -sha256 -days 30 -nodes \
  -subj '/CN=Research Operations TEST CA' \
  -keyout "$output/ca.key" -out "$output/ca.crt"

openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -subj "/CN=$proxy_dns" -addext "subjectAltName=DNS:$proxy_dns" \
  -addext 'extendedKeyUsage=serverAuth' \
  -keyout "$output/proxy.key" -out "$output/proxy.csr"
openssl x509 -req -sha256 -days 14 -copy_extensions copy \
  -in "$output/proxy.csr" -CA "$output/ca.crt" -CAkey "$output/ca.key" \
  -CAcreateserial -out "$output/proxy.crt"

openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -subj '/CN=postgres' -addext 'subjectAltName=DNS:postgres' \
  -addext 'extendedKeyUsage=serverAuth' \
  -keyout "$output/postgres.key" -out "$output/postgres.csr"
openssl x509 -req -sha256 -days 14 -copy_extensions copy \
  -in "$output/postgres.csr" -CA "$output/ca.crt" -CAkey "$output/ca.key" \
  -CAcreateserial -out "$output/postgres.crt"

openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -subj '/CN=research-ops-test-client' -addext 'extendedKeyUsage=clientAuth' \
  -keyout "$output/ops-client.key" -out "$output/ops-client.csr"
openssl x509 -req -sha256 -days 14 -copy_extensions copy \
  -in "$output/ops-client.csr" -CA "$output/ca.crt" -CAkey "$output/ca.key" \
  -CAcreateserial -out "$output/ops-client.crt"

chmod 0600 "$output"/*.key
printf '%s\n' 'TEST-ONLY PKI: never promote these keys or certificates.' > "$output/TEST_ONLY"
