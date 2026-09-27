// Request handlers, independent of Supabase so they can be unit-tested.
//
// ingest:      device -> signed batch of events (+ optional heartbeat)
// stale-check: scheduled job -> alert once when a unit goes silent

import {
  deliver, deviceMessage, eventMessage, subscribed,
  type AlertLogEntry, type EventType, type Notifier,
} from "./alerts.ts";
import { timingSafeEqualHex, verifySignedBody } from "./hmac.ts";
import type { EventRow, Store } from "./store.ts";

const MAX_BODY_BYTES = 256 * 1024;
const MAX_EVENTS = 200;
const DEVICE_EVENT_TYPES = new Set<EventType>(["TAILGATE", "MULTIPLE", "SENSOR_ERROR", "PASS", "DOOR_OPEN", "BOOM_OVERTIME"]);
const STATES = new Set(["ASSERT", "CLEAR", "PULSE"]);
const DEVICE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

export interface Deps {
  store: Store;
  notifier: Notifier;
  now: () => number; // ms since epoch
  log: (msg: string, fields?: Record<string, unknown>) => void;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type Parsed = { rows: EventRow[]; seqs: number[]; heartbeat: Record<string, unknown> | null };

/** Validate the decoded body against the device record. Throws a string on invalid input. */
function parseBody(raw: unknown, dev: { id: string; tenant_id: string; site_id: string }, nowMs: number): Parsed {
  if (typeof raw !== "object" || raw === null) throw "body must be an object";
  const b = raw as Record<string, unknown>;
  if (b.deviceId !== dev.id) throw "deviceId does not match X-MK-Device";
  if (!Array.isArray(b.events)) throw "events must be an array";
  if (b.events.length > MAX_EVENTS) throw `at most ${MAX_EVENTS} events per batch`;
  const hb = b.heartbeat;
  if (hb !== null && hb !== undefined && (typeof hb !== "object" || Array.isArray(hb))) throw "heartbeat must be an object or null";

  const rows: EventRow[] = [];
  const seqs: number[] = [];
  const seen = new Set<number>();
  for (const [i, e] of (b.events as unknown[]).entries()) {
    if (typeof e !== "object" || e === null) throw `events[${i}] must be an object`;
    const ev = e as Record<string, unknown>;
    const seq = ev.seq;
    if (typeof seq !== "number" || !Number.isSafeInteger(seq) || seq <= 0) throw `events[${i}].seq invalid`;
    if (seen.has(seq)) throw `events[${i}].seq duplicated in batch`;
    if (!DEVICE_EVENT_TYPES.has(ev.type as EventType)) throw `events[${i}].type invalid`;
    if (!STATES.has(ev.state as string)) throw `events[${i}].state invalid`;
    const at = ev.occurredAt;
    // occurredAt is unix seconds (float). Allow 5 min clock skew ahead; no lower bound
    // other than 1 Jan 2025, because backlog from a long outage is legitimate.
    if (typeof at !== "number" || !Number.isFinite(at) || at * 1000 > nowMs + 300_000 || at < 1_735_689_600) {
      throw `events[${i}].occurredAt invalid`;
    }
    const input = ev.input;
    if (input !== undefined && (typeof input !== "string" || input.length > 64)) throw `events[${i}].input invalid`;
    const extra = ev.extra;
    if (extra !== undefined && (typeof extra !== "object" || extra === null || JSON.stringify(extra).length > 2048)) {
      throw `events[${i}].extra invalid`;
    }
    seen.add(seq);
    seqs.push(seq);
    rows.push({
      tenant_id: dev.tenant_id,           // tenancy always from the device record,
      site_id: dev.site_id,               // never from the request body
      device_id: dev.id,
      seq,
      event_type: ev.type as string,
      state: ev.state as string,
      input_name: (input as string | undefined) ?? null,
      occurred_at: new Date(at * 1000).toISOString(),
      payload: { location: typeof ev.location === "string" ? ev.location.slice(0, 64) : null, extra: extra ?? null },
    });
  }
  return { rows, seqs, heartbeat: (hb as Record<string, unknown> | null | undefined) ?? null };
}

export async function handleIngest(req: Request, deps: Deps): Promise<Response> {
  if (req.method !== "POST") return json(405, { error: "method not allowed" });

  const deviceId = req.headers.get("x-mk-device") ?? "";
  if (!DEVICE_ID_RE.test(deviceId)) return json(401, { error: "unauthorised" });

  const len = Number(req.headers.get("content-length") ?? "0");
  if (len > MAX_BODY_BYTES) return json(413, { error: "body too large" });
  const body = new Uint8Array(await req.arrayBuffer());
  if (body.length > MAX_BODY_BYTES) return json(413, { error: "body too large" });

  const dev = await deps.store.getDevice(deviceId);
  if (!dev || !dev.secret) {
    deps.log("ingest reject", { deviceId, reason: "unknown device or no secret" });
    return json(401, { error: "unauthorised" });           // same answer as a bad signature
  }
  const v = await verifySignedBody(
    dev.secret, req.headers.get("x-mk-timestamp"), req.headers.get("x-mk-signature"),
    body, Math.floor(deps.now() / 1000),
  );
  if (!v.ok) {
    deps.log("ingest reject", { deviceId, reason: v.reason });
    return json(401, { error: "unauthorised" });
  }
  if (!dev.enabled) return json(403, { error: "device disabled" });

  let parsed: Parsed;
  try {
    parsed = parseBody(JSON.parse(new TextDecoder().decode(body)), dev, deps.now());
  } catch (e) {
    const reason = typeof e === "string" ? e : "malformed JSON";
    deps.log("ingest invalid", { deviceId, reason });
    return json(400, { error: reason });
  }

  // Store first. Only after the rows are durable do we confirm seqs to the Pi.
  const inserted = await deps.store.insertEvents(parsed.rows);
  await deps.store.touchDevice(dev.id, parsed.heartbeat);

  // Alerts: only for rows inserted by THIS request, so a re-sent batch never re-alerts.
  const logs: AlertLogEntry[] = [];
  const needAlerts = inserted.some((e) => e.state === "ASSERT") || dev.stale_alerted;
  if (needAlerts) {
    const contacts = await deps.store.contactsFor(dev.site_id);
    for (const ev of inserted) {
      if (ev.state !== "ASSERT") continue;
      const to = subscribed(contacts, ev.event_type);
      if (to.length === 0) continue;
      logs.push(...await deliver(deps.notifier, to,
        { tenant_id: dev.tenant_id, event_id: ev.id, device_id: dev.id, kind: "event" },
        eventMessage(dev, ev)));
    }
    if (dev.stale_alerted) {
      logs.push(...await deliver(deps.notifier, subscribed(contacts, "DEVICE_OFFLINE"),
        { tenant_id: dev.tenant_id, event_id: null, device_id: dev.id, kind: "device_recovered" },
        deviceMessage(dev, "device_recovered")));
    }
  }
  if (logs.length) {
    try {
      await deps.store.logAlerts(logs);
    } catch (e) {
      deps.log("alert log write failed", { deviceId, error: String(e) }); // events already stored; do not fail the batch
    }
  }

  deps.log("ingest ok", { deviceId, received: parsed.seqs.length, inserted: inserted.length, alerts: logs.length });
  return json(200, { accepted: parsed.seqs, inserted: inserted.length });
}

export async function handleStaleCheck(
  req: Request, deps: Deps, cronSecret: string | undefined, staleMinutes: number,
): Promise<Response> {
  if (!cronSecret || cronSecret.length < 32) return json(500, { error: "CRON_SECRET not configured" });
  const auth = req.headers.get("authorization") ?? "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!timingSafeEqualHex(given, cronSecret)) return json(401, { error: "unauthorised" });

  const cutoff = new Date(deps.now() - staleMinutes * 60_000).toISOString();
  const stale = await deps.store.listNewlyStale(cutoff);
  let alerts = 0;
  for (const dev of stale) {
    const contacts = subscribed(await deps.store.contactsFor(dev.site_id), "DEVICE_OFFLINE");
    const logs = await deliver(deps.notifier, contacts,
      { tenant_id: dev.tenant_id, event_id: null, device_id: dev.id, kind: "device_offline" },
      deviceMessage(dev, "device_offline"));
    await deps.store.markStale(dev.id);   // mark even with no contacts, so it alerts once per outage
    if (logs.length) await deps.store.logAlerts(logs);
    alerts += logs.length;
  }
  deps.log("stale check", { cutoff, stale: stale.length, alerts });
  return json(200, { stale: stale.map((d) => d.id), alerts });
}
