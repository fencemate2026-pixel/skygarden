// Shared types, alert wording and delivery for MagicKeys Detect.
//
// Wording rule: alerts say "possible" - the sensor flags, a person decides.
// Delivery providers are optional. If a provider's env vars are absent the
// attempt is logged as "not_configured" - never reported as sent.

export type EventType =
  | "TAILGATE" | "MULTIPLE" | "SENSOR_ERROR" | "PASS" | "DOOR_OPEN" | "BOOM_OVERTIME";
export type EventState = "ASSERT" | "CLEAR" | "PULSE";
export type DeliveryStatus = "sent" | "failed" | "not_configured";

export interface DeviceRecord {
  id: string;
  tenant_id: string;
  site_id: string;
  label: string;
  kind: string;
  enabled: boolean;
  stale_alerted: boolean;
  last_seen_at: string | null;
  secret: string | null;
  site_name: string;
  timezone: string;
}

export interface Contact {
  id: string;
  name: string;
  email: string | null;
  phone_e164: string | null;
  event_types: string[];
}

export interface StoredEvent {
  id: number;
  seq: number;
  event_type: EventType;
  state: EventState;
  occurred_at: string;
  input_name: string | null;
}

export interface AlertLogEntry {
  tenant_id: string;
  event_id: number | null;
  device_id: string;
  contact_id: string;
  channel: "email" | "sms";
  kind: "event" | "device_offline" | "device_recovered";
  status: DeliveryStatus;
  detail: string | null;
}

export interface Notifier {
  email(to: string, subject: string, text: string): Promise<{ status: DeliveryStatus; detail: string | null }>;
  sms(to: string, text: string): Promise<{ status: DeliveryStatus; detail: string | null }>;
}

const LABELS: Record<EventType, string> = {
  TAILGATE: "Possible tailgate",
  MULTIPLE: "Possible group entry (more than one person in the door zone)",
  SENSOR_ERROR: "Sensor reports it cannot detect (check lighting/obstruction)",
  PASS: "Entry",
  DOOR_OPEN: "Door open",
  BOOM_OVERTIME: "Boom arm up longer than normal",
};

export function formatLocal(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-AU", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    weekday: "short",
    day: "numeric",
    month: "short",
    hour12: false,
  }).format(new Date(iso));
}

export function eventMessage(dev: DeviceRecord, ev: StoredEvent): { subject: string; text: string } {
  const what = LABELS[ev.event_type] ?? ev.event_type;
  const when = formatLocal(ev.occurred_at, dev.timezone);
  const subject = `${dev.site_name} - ${dev.label}: ${what}`;
  const text = `${dev.site_name} - ${dev.label}: ${what} at ${when}. Review in MagicKeys.`;
  return { subject, text };
}

export function deviceMessage(dev: DeviceRecord, kind: "device_offline" | "device_recovered"): { subject: string; text: string } {
  const seen = dev.last_seen_at ? formatLocal(dev.last_seen_at, dev.timezone) : "never";
  if (kind === "device_offline") {
    const subject = `${dev.site_name} - ${dev.label}: unit offline`;
    return { subject, text: `${subject}. Last contact ${seen}. Alerts from this unit are delayed until it reconnects.` };
  }
  const subject = `${dev.site_name} - ${dev.label}: unit back online`;
  return { subject, text: `${subject}. Any alerts held during the outage are being delivered now.` };
}

/** Contacts whose subscription covers this key, e.g. "TAILGATE" or "DEVICE_OFFLINE". */
export function subscribed(contacts: Contact[], key: string): Contact[] {
  return contacts.filter((c) => c.event_types.includes(key));
}

/** Send to every channel a contact has; returns log entries (never throws). */
export async function deliver(
  notifier: Notifier,
  contacts: Contact[],
  base: Omit<AlertLogEntry, "contact_id" | "channel" | "status" | "detail">,
  msg: { subject: string; text: string },
): Promise<AlertLogEntry[]> {
  const jobs: Promise<AlertLogEntry>[] = [];
  for (const c of contacts) {
    if (c.email) {
      jobs.push(notifier.email(c.email, msg.subject, msg.text)
        .catch((e) => ({ status: "failed" as const, detail: String(e) }))
        .then((r) => ({ ...base, contact_id: c.id, channel: "email" as const, ...r })));
    }
    if (c.phone_e164) {
      jobs.push(notifier.sms(c.phone_e164, msg.text)
        .catch((e) => ({ status: "failed" as const, detail: String(e) }))
        .then((r) => ({ ...base, contact_id: c.id, channel: "sms" as const, ...r })));
    }
  }
  return await Promise.all(jobs);
}

/** Email via Resend, SMS via Twilio. Provider API shapes are UNVERIFIED against a live account. */
export function envNotifier(env: { get(k: string): string | undefined }, fetchFn: typeof fetch = fetch): Notifier {
  return {
    async email(to, subject, text) {
      const key = env.get("RESEND_API_KEY");
      const from = env.get("ALERT_FROM_EMAIL");
      if (!key || !from) return { status: "not_configured", detail: "RESEND_API_KEY/ALERT_FROM_EMAIL unset" };
      const res = await fetchFn("https://api.resend.com/emails", {
        method: "POST",
        headers: { "Authorization": `Bearer ${key}`, "Content-Type": "application/json" },
        body: JSON.stringify({ from, to: [to], subject, text }),
      });
      return res.ok
        ? { status: "sent", detail: null }
        : { status: "failed", detail: `resend ${res.status}: ${(await res.text()).slice(0, 200)}` };
    },
    async sms(to, text) {
      const sid = env.get("TWILIO_ACCOUNT_SID");
      const token = env.get("TWILIO_AUTH_TOKEN");
      const from = env.get("TWILIO_FROM");
      if (!sid || !token || !from) return { status: "not_configured", detail: "TWILIO_* unset" };
      const res = await fetchFn(`https://api.twilio.com/2010-04-01/Accounts/${sid}/Messages.json`, {
        method: "POST",
        headers: {
          "Authorization": `Basic ${btoa(`${sid}:${token}`)}`,
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams({ To: to, From: from, Body: text.slice(0, 600) }),
      });
      return res.ok
        ? { status: "sent", detail: null }
        : { status: "failed", detail: `twilio ${res.status}: ${(await res.text()).slice(0, 200)}` };
    },
  };
}
