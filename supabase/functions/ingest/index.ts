// Edge function: POST /functions/v1/ingest
// Devices authenticate with their own HMAC signature, not a Supabase JWT,
// so deploy with:  supabase functions deploy ingest --no-verify-jwt
import { envNotifier } from "../_shared/alerts.ts";
import { handleIngest } from "../_shared/handlers.ts";
import { SupabaseStore } from "../_shared/store.ts";

const url = Deno.env.get("SUPABASE_URL");
const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
if (!url || !key) throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not available");

const deps = {
  store: new SupabaseStore(url, key),
  notifier: envNotifier(Deno.env),
  now: () => Date.now(),
  log: (msg: string, fields: Record<string, unknown> = {}) =>
    console.log(JSON.stringify({ fn: "ingest", msg, ...fields })),
};

Deno.serve(async (req) => {
  try {
    return await handleIngest(req, deps);
  } catch (e) {
    // Storage failure: the Pi keeps the batch and retries, so nothing is lost.
    deps.log("ingest error", { error: String(e) });
    return new Response(JSON.stringify({ error: "server error" }), {
      status: 500, headers: { "Content-Type": "application/json" },
    });
  }
});
