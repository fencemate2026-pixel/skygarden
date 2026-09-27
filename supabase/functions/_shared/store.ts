// Data access for the edge functions. The handlers depend only on the Store
// interface, so they are unit-tested with an in-memory fake; SupabaseStore is
// the production implementation (service role, server-side only).

import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2";
import type { AlertLogEntry, Contact, DeviceRecord, StoredEvent } from "./alerts.ts";

export interface EventRow {
  tenant_id: string;
  site_id: string;
  device_id: string;
  seq: number;
  event_type: string;
  state: string;
  input_name: string | null;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface Store {
  getDevice(id: string): Promise<DeviceRecord | null>;
  /** Insert, ignoring (device_id, seq) duplicates. Returns ONLY newly inserted rows. */
  insertEvents(rows: EventRow[]): Promise<StoredEvent[]>;
  touchDevice(id: string, heartbeat: Record<string, unknown> | null): Promise<void>;
  contactsFor(siteId: string): Promise<Contact[]>;
  logAlerts(entries: AlertLogEntry[]): Promise<void>;
  /** Enabled devices silent since before `cutoffIso` and not yet alerted. */
  listNewlyStale(cutoffIso: string): Promise<DeviceRecord[]>;
  markStale(id: string): Promise<void>;
}

export class SupabaseStore implements Store {
  private db: SupabaseClient;

  constructor(url: string, serviceRoleKey: string) {
    this.db = createClient(url, serviceRoleKey, { auth: { persistSession: false } });
  }

  private static flatten(d: Record<string, any>): DeviceRecord {
    return {
      id: d.id, tenant_id: d.tenant_id, site_id: d.site_id, label: d.label, kind: d.kind,
      enabled: d.enabled, stale_alerted: d.stale_alerted, last_seen_at: d.last_seen_at,
      secret: d.device_secrets?.secret ?? null,
      site_name: d.sites?.name ?? "", timezone: d.sites?.timezone ?? "Australia/Melbourne",
    };
  }

  async getDevice(id: string): Promise<DeviceRecord | null> {
    const { data, error } = await this.db
      .from("devices")
      .select("id,tenant_id,site_id,label,kind,enabled,stale_alerted,last_seen_at,device_secrets(secret),sites(name,timezone)")
      .eq("id", id)
      .maybeSingle();
    if (error) throw new Error(`getDevice: ${error.message}`);
    return data ? SupabaseStore.flatten(data) : null;
  }

  async insertEvents(rows: EventRow[]): Promise<StoredEvent[]> {
    if (rows.length === 0) return [];
    const { data, error } = await this.db
      .from("events")
      .upsert(rows, { onConflict: "device_id,seq", ignoreDuplicates: true })
      .select("id,seq,event_type,state,occurred_at,input_name");
    if (error) throw new Error(`insertEvents: ${error.message}`);
    return (data ?? []) as StoredEvent[];
  }

  async touchDevice(id: string, heartbeat: Record<string, unknown> | null): Promise<void> {
    const patch: Record<string, unknown> = { last_seen_at: new Date().toISOString(), stale_alerted: false };
    if (heartbeat) patch.last_heartbeat = heartbeat;
    const { error } = await this.db.from("devices").update(patch).eq("id", id);
    if (error) throw new Error(`touchDevice: ${error.message}`);
  }

  async contactsFor(siteId: string): Promise<Contact[]> {
    const { data, error } = await this.db
      .from("alert_contacts")
      .select("id,name,email,phone_e164,event_types")
      .eq("site_id", siteId)
      .eq("enabled", true);
    if (error) throw new Error(`contactsFor: ${error.message}`);
    return (data ?? []) as Contact[];
  }

  async logAlerts(entries: AlertLogEntry[]): Promise<void> {
    if (entries.length === 0) return;
    const { error } = await this.db.from("alert_log").insert(entries);
    if (error) throw new Error(`logAlerts: ${error.message}`);
  }

  async listNewlyStale(cutoffIso: string): Promise<DeviceRecord[]> {
    const { data, error } = await this.db
      .from("devices")
      .select("id,tenant_id,site_id,label,kind,enabled,stale_alerted,last_seen_at,sites(name,timezone)")
      .eq("enabled", true)
      .eq("stale_alerted", false)
      .lt("last_seen_at", cutoffIso);
    if (error) throw new Error(`listNewlyStale: ${error.message}`);
    return (data ?? []).map((d) => SupabaseStore.flatten(d));
  }

  async markStale(id: string): Promise<void> {
    const { error } = await this.db.from("devices").update({ stale_alerted: true }).eq("id", id);
    if (error) throw new Error(`markStale: ${error.message}`);
  }
}
