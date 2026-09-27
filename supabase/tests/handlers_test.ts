// Run: deno test --allow-read supabase/tests/handlers_test.ts
import { assert, assertEquals } from "jsr:@std/assert@1";
import { envNotifier, type Contact, type DeviceRecord, type Notifier, type AlertLogEntry, type StoredEvent } from "../functions/_shared/alerts.ts";
import { handleIngest, handleStaleCheck, type Deps } from "../functions/_shared/handlers.ts";
import { hmacHex, verifySignedBody } from "../functions/_shared/hmac.ts";
import type { EventRow, Store } from "../functions/_shared/store.ts";

const SECRET = "s".repeat(40);
const enc = new TextEncoder();
const NOW = 1_790_500_000_000; // ms

// ---------------------------------------------------------------- fakes ----
class FakeStore implements Store {
  devices = new Map<string, DeviceRecord>();
  events: (EventRow & { id: number })[] = [];
  contacts: Contact[] = [];
  alertLog: AlertLogEntry[] = [];
  heartbeats: unknown[] = [];

  async getDevice(id: string) { const d = this.devices.get(id); return d ? { ...d } : null; }
  async insertEvents(rows: EventRow[]): Promise<StoredEvent[]> {
    const out: StoredEvent[] = [];
    for (const r of rows) {
      if (this.events.some((e) => e.device_id === r.device_id && e.seq === r.seq)) continue; // ON CONFLICT DO NOTHING
      const id = this.events.length + 1;
      this.events.push({ ...r, id });
      out.push({ id, seq: r.seq, event_type: r.event_type as any, state: r.state as any, occurred_at: r.occurred_at, input_name: r.input_name });
    }
    return out;
  }
  async touchDevice(id: string, hb: Record<string, unknown> | null) {
    const d = this.devices.get(id)!; d.stale_alerted = false; d.last_seen_at = new Date(NOW).toISOString();
    if (hb) this.heartbeats.push(hb);
  }
  async contactsFor(_siteId: string) { return this.contacts; }
  async logAlerts(e: AlertLogEntry[]) { this.alertLog.push(...e); }
  async listNewlyStale(cutoff: string) {
    return [...this.devices.values()].filter((d) => d.enabled && !d.stale_alerted && d.last_seen_at !== null && d.last_seen_at < cutoff);
  }
  async markStale(id: string) { this.devices.get(id)!.stale_alerted = true; }
}

class FakeNotifier implements Notifier {
  sent: { ch: string; to: string; text: string }[] = [];
  async email(to: string, _s: string, text: string) { this.sent.push({ ch: "email", to, text }); return { status: "sent" as const, detail: null }; }
  async sms(to: string, text: string) { this.sent.push({ ch: "sms", to, text }); return { status: "sent" as const, detail: null }; }
}

function setup() {
  const store = new FakeStore();
  store.devices.set("skygarden-foyer-01", {
    id: "skygarden-foyer-01", tenant_id: "T-SKY", site_id: "S-SKY", label: "Main foyer", kind: "foyer_optex",
    enabled: true, stale_alerted: false, last_seen_at: null, secret: SECRET, site_name: "Sky Garden", timezone: "Australia/Melbourne",
  });
  store.contacts = [
    { id: "c-bm", name: "Building manager", email: "bm@example.com", phone_e164: "+61400000000", event_types: ["TAILGATE", "MULTIPLE"] },
    { id: "c-rjl", name: "RJL ops", email: "ops@example.com", phone_e164: null, event_types: ["SENSOR_ERROR", "DEVICE_OFFLINE"] },
  ];
  const notifier = new FakeNotifier();
  const logs: string[] = [];
  const deps: Deps = { store, notifier, now: () => NOW, log: (m) => logs.push(m) };
  return { store, notifier, deps, logs };
}

async function signedRequest(payload: unknown, opts: { secret?: string; tsOffset?: number; device?: string } = {}) {
  const body = enc.encode(JSON.stringify(payload));
  const ts = Math.floor(NOW / 1000) + (opts.tsOffset ?? 0);
  const prefix = enc.encode(`${ts}.`);
  const msg = new Uint8Array(prefix.length + body.length); msg.set(prefix); msg.set(body, prefix.length);
  return new Request("https://x/functions/v1/ingest", {
    method: "POST", body,
    headers: {
      "Content-Type": "application/json",
      "X-MK-Device": opts.device ?? "skygarden-foyer-01",
      "X-MK-Timestamp": String(ts),
      "X-MK-Signature": await hmacHex(opts.secret ?? SECRET, msg),
    },
  });
}

const ev = (seq: number, type = "TAILGATE", state = "ASSERT") =>
  ({ seq, type, state, input: "optex_tailgating_1", location: "Main foyer", occurredAt: NOW / 1000 - 5 });

// ---------------------------------------------------------------- tests ----
Deno.test("HMAC matches the Python signer (cross-language fixture)", async () => {
  const f = JSON.parse(await Deno.readTextFile(new URL("../../tests/fixtures/hmac_vector.json", import.meta.url)));
  const r = await verifySignedBody(f.secret, String(f.timestamp), f.signature, enc.encode(f.body), f.timestamp);
  assertEquals(r, { ok: true });
  const tampered = await verifySignedBody(f.secret, String(f.timestamp), f.signature, enc.encode(f.body + " "), f.timestamp);
  assertEquals(tampered.ok, false);
});

Deno.test("valid batch is stored, confirmed and alerts the subscribed contact on both channels", async () => {
  const { store, notifier, deps } = setup();
  const res = await handleIngest(await signedRequest({ deviceId: "skygarden-foyer-01", events: [ev(1), ev(2, "PASS", "PULSE")], heartbeat: { uptimeS: 5 } }), deps);
  assertEquals(res.status, 200);
  assertEquals((await res.json()).accepted, [1, 2]);
  assertEquals(store.events.length, 2);
  assertEquals(store.events[0].tenant_id, "T-SKY");
  assertEquals(notifier.sent.map((s) => s.ch).sort(), ["email", "sms"]);  // BM only, not RJL ops
  assert(notifier.sent[0].text.includes("Possible tailgate"));
  assertEquals(store.alertLog.length, 2);
  assertEquals(store.heartbeats.length, 1);
});

Deno.test("re-sent batch is confirmed but never re-alerts", async () => {
  const { store, notifier, deps } = setup();
  const payload = { deviceId: "skygarden-foyer-01", events: [ev(7)], heartbeat: null };
  await handleIngest(await signedRequest(payload), deps);
  const res = await handleIngest(await signedRequest(payload), deps);
  assertEquals(res.status, 200);
  assertEquals((await res.json()).accepted, [7]);
  assertEquals(store.events.length, 1);
  assertEquals(notifier.sent.length, 2);
});

Deno.test("bad signature, stale timestamp and unknown device all get the same 401", async () => {
  const { store, deps } = setup();
  const p = { deviceId: "skygarden-foyer-01", events: [ev(1)], heartbeat: null };
  for (const req of [
    await signedRequest(p, { secret: "x".repeat(40) }),
    await signedRequest(p, { tsOffset: -301 }),
    await signedRequest({ ...p, deviceId: "ghost-01" }, { device: "ghost-01" }),
  ]) {
    const res = await handleIngest(req, deps);
    assertEquals(res.status, 401);
    assertEquals(await res.json(), { error: "unauthorised" });
  }
  assertEquals(store.events.length, 0);
});

Deno.test("disabled device refused; body/device mismatch and bad events rejected", async () => {
  const { store, deps } = setup();
  store.devices.get("skygarden-foyer-01")!.enabled = false;
  assertEquals((await handleIngest(await signedRequest({ deviceId: "skygarden-foyer-01", events: [], heartbeat: null }), deps)).status, 403);
  store.devices.get("skygarden-foyer-01")!.enabled = true;
  for (const bad of [
    { deviceId: "someone-else", events: [], heartbeat: null },
    { deviceId: "skygarden-foyer-01", events: [{ ...ev(1), type: "UNLOCK" }], heartbeat: null },
    { deviceId: "skygarden-foyer-01", events: [ev(1), ev(1)], heartbeat: null },
    { deviceId: "skygarden-foyer-01", events: [{ ...ev(1), occurredAt: NOW / 1000 + 3600 }], heartbeat: null },
    { deviceId: "skygarden-foyer-01", events: [{ ...ev(1), seq: 0 }], heartbeat: null },
  ]) {
    assertEquals((await handleIngest(await signedRequest(bad), deps)).status, 400);
  }
  assertEquals(store.events.length, 0);
});

Deno.test("CLEAR and PULSE events do not alert", async () => {
  const { notifier, deps } = setup();
  await handleIngest(await signedRequest({ deviceId: "skygarden-foyer-01", events: [ev(1, "TAILGATE", "CLEAR"), ev(2, "PASS", "PULSE")], heartbeat: null }), deps);
  assertEquals(notifier.sent.length, 0);
});

Deno.test("stale check alerts once per outage, then recovery alert on next contact", async () => {
  const { store, notifier, deps } = setup();
  store.devices.get("skygarden-foyer-01")!.last_seen_at = new Date(NOW - 30 * 60_000).toISOString();
  const cron = "c".repeat(40);
  const mk = () => new Request("https://x/functions/v1/stale-check", { method: "POST", headers: { Authorization: `Bearer ${cron}` } });

  assertEquals((await handleStaleCheck(new Request("https://x", { method: "POST" }), deps, cron, 10)).status, 401);
  const r1 = await handleStaleCheck(mk(), deps, cron, 10);
  assertEquals((await r1.json()).stale, ["skygarden-foyer-01"]);
  const r2 = await handleStaleCheck(mk(), deps, cron, 10);
  assertEquals((await r2.json()).stale, []);                       // no repeat alert
  assertEquals(notifier.sent.filter((s) => s.text.includes("offline")).length, 1);  // RJL ops email only

  await handleIngest(await signedRequest({ deviceId: "skygarden-foyer-01", events: [], heartbeat: { uptimeS: 1 } }), deps);
  assertEquals(notifier.sent.filter((s) => s.text.includes("back online")).length, 1);
  assertEquals(store.devices.get("skygarden-foyer-01")!.stale_alerted, false);
});

Deno.test("unconfigured providers are logged as not_configured, never as sent", async () => {
  const n = envNotifier({ get: () => undefined }, () => { throw new Error("must not call network"); });
  assertEquals((await n.email("a@b.c", "s", "t")).status, "not_configured");
  assertEquals((await n.sms("+61400000000", "t")).status, "not_configured");
});
