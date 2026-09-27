#!/usr/bin/env bash
# Spins up a throwaway local PostgreSQL, applies the Supabase stub + migration,
# then runs rls_test.sql. Needs PostgreSQL 15+ binaries on PATH or in /usr/lib/postgresql/*/bin.
set -euo pipefail
if [ "$(id -u)" = "0" ]; then echo "Run as a non-root user: initdb refuses root. e.g. su nobody -s /bin/bash -c "$0""; exit 1; fi
here="$(cd "$(dirname "$0")" && pwd)"
bin="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"; export PATH="$bin:$PATH"
tmp="$(mktemp -d)"; trap 'pg_ctl -D "$tmp/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
initdb -D "$tmp/data" -U postgres >/dev/null
pg_ctl -D "$tmp/data" -o "-k $tmp -c listen_addresses=''" -l "$tmp/log" start >/dev/null
psql() { command psql -h "$tmp" -U postgres -v ON_ERROR_STOP=1 -q "$@"; }
psql -c "create database t"
psql -d t -f "$here/supabase_stub.sql"
for m in "$here"/../migrations/*.sql; do psql -d t -f "$m"; done
psql -d t -f "$here/rls_test.sql" 2>&1 | grep -E "NOTICE|ERROR" | sed 's/^psql:[^ ]* //'
echo "SQL TESTS COMPLETE"
