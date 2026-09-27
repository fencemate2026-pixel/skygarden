# MagicKeys Detect

RJL Commercial Group's detection-only monitoring product, first site **Sky Garden** (foyer anti-tailgating + LG boom).
Same MagicKeys front end as Balwyn, re-skinned, on its **own separate backend**.

**This system drives nothing.** No relays, no door release, no gate commands. The Pi claims GPIO lines as inputs only,
the config loader refuses any output/relay/pulse/unlock key, and the database has no command or trigger table.
Fire egress and the building's access control are untouched.

## Layout
| Path | What it is |
|---|---|
| `pi-agent/` | Python agent on the site Pi: reads Optex OV-102 relay outputs, stores events locally, sends signed batches, heartbeats, systemd watchdog |
| `config/` | Site config examples (no secrets, ever) |
| `supabase/migrations/` | Multi-tenant schema with row-level security |
| `supabase/functions/ingest` | Receives signed device batches, stores them once, alerts subscribed contacts |
| `supabase/functions/stale-check` | Scheduled: alerts once when a unit goes silent, recovery notice on return |
| `supabase/tests/` | Handler tests (Deno), SQL/RLS tests (PostgreSQL), wire-compat server |
| `frontend/` | Where the re-skinned MagicKeys portal goes (see `frontend/README.md`) |
| `docs/` | Wiring, Pi install, backend deploy |

## Test everything
```
scripts/test_all.sh      # python3+pytest, deno, PostgreSQL 15+ binaries required
```

## Evidence status (27 Sep 2026)
| Item | Status |
|---|---|
| Agent logic: debounce, events, store-and-forward, signing, backoff, watchdog gating, config interlocks | VERIFIED - 33 automated tests, fake GPIO |
| Python agent -> real TypeScript ingest handler over HTTP | VERIFIED - wire-compat test |
| Schema + RLS tenant isolation, secrets hidden, anon denied | VERIFIED - PostgreSQL 16 with a Supabase auth stub |
| Edge functions type-check | VERIFIED - `deno check` |
| Deployed to a real Supabase project | NOT DONE |
| Real Pi 5 + lgpio + opto input board + Optex OV-102 | UNVERIFIED - bench test required (`docs/WIRING.md`) |
| Resend email / Twilio SMS delivery | UNVERIFIED - no accounts configured; unconfigured = logged `not_configured`, never "sent" |
| LG boom camera (arm-up timing) | NOT IN THIS REPO YET - backend already accepts `BOOM_OVERTIME` |
| Front end | NOT IN THIS REPO YET - needs the Balwyn v53 source to fork |
