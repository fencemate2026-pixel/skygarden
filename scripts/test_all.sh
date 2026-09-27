#!/usr/bin/env bash
# Runs every automated test in the repo. Needs: python3 + pytest, deno, PostgreSQL 15+ binaries.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
echo "== Python agent (includes Python<->TypeScript wire test) =="
(cd "$root/pi-agent" && python3 -m pytest -q)
echo "== Edge function type check =="
(cd "$root/supabase" && deno check functions/ingest/index.ts functions/stale-check/index.ts)
echo "== Edge function handler tests =="
(cd "$root" && deno test --allow-read supabase/tests/handlers_test.ts)
echo "== SQL schema + RLS tests =="
if [ "$(id -u)" = "0" ]; then
  su nobody -s /bin/bash -c "bash '$root/supabase/tests/run_sql_tests.sh'"
else
  bash "$root/supabase/tests/run_sql_tests.sh"
fi
