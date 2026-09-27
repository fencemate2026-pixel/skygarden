// Edge function: POST /functions/v1/stale-check  (called by pg_cron every 5 min)
// Auth: "Authorization: Bearer <CRON_SECRET>". Deploy with --no-verify-jwt.
import { envNotifier } from "../_shared/alerts.ts";
import { handleStaleCheck } from "../_shared/handlers.ts";
import { SupabaseStore } from "../_shared/store.ts";

const url = Deno.env.get("SUPABASE_URL");
const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
if (!url || !key) throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not available");
const staleMinutes = Number(Deno.env.get("STALE_MINUTES") ?? "10");

const deps = {
  store: new SupabaseStore(url, key),
  notifier: envNotifier(Deno.env),
  now: () => Date.now(),
  log: (msg: string, fields: Record<string, unknown> = {}) =>
    console.log(JSON.stringify({ fn: "stale-check", msg, ...fields })),
};

Deno.serve(async (req) => {
  try {
    return await handleStaleCheck(req, deps, Deno.env.get("CRON_SECRET"), staleMinutes);
  } catch (e) {
    deps.log("stale-check error", { error: String(e) });
    return new Response(JSON.stringify({ error: "server error" }), {
      status: 500, headers: { "Content-Type": "application/json" },
    });
  }
});
