// Test-only HTTP server: the real ingest handler over a real socket with an
// in-memory store, so the Python agent's wire format is checked end to end.
// Usage: deno run --allow-net --allow-env supabase/tests/wire_server.ts  (PORT env)
import type { Contact, DeviceRecord, StoredEvent } from "../functions/_shared/alerts.ts";
import { handleIngest } from "../functions/_shared/handlers.ts";
import type { EventRow, Store } from "../functions/_shared/store.ts";

const dev: DeviceRecord = {
  id: "skygarden-foyer-01", tenant_id: "T", site_id: "S", label: "Main foyer", kind: "foyer_optex",
  enabled: true, stale_alerted: false, last_seen_at: null, secret: "s".repeat(40),
  site_name: "Sky Garden", timezone: "Australia/Melbourne",
};
const rows: (EventRow & { id: number })[] = [];
const store: Store = {
  getDevice: async (id) => (id === dev.id ? dev : null),
  insertEvents: async (rs) => {
    const out: StoredEvent[] = [];
    for (const r of rs) {
      if (rows.some((x) => x.seq === r.seq)) continue;
      rows.push({ ...r, id: rows.length + 1 });
      out.push({ id: rows.length, seq: r.seq, event_type: r.event_type as any, state: r.state as any, occurred_at: r.occurred_at, input_name: r.input_name });
    }
    return out;
  },
  touchDevice: async () => {},
  contactsFor: async (): Promise<Contact[]> => [],
  logAlerts: async () => {},
  listNewlyStale: async () => [],
  markStale: async () => {},
};
const port = Number(Deno.env.get("PORT") ?? "8799");
Deno.serve({ port, hostname: "127.0.0.1", onListen: () => console.log("READY") }, (req) =>
  handleIngest(req, { store, notifier: { email: async () => ({ status: "sent", detail: null }), sms: async () => ({ status: "sent", detail: null }) }, now: () => Date.now(), log: () => {} }));
