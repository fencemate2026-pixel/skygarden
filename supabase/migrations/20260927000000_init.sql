-- MagicKeys Detect: initial schema.
-- Separate Supabase project from Balwyn and Thomastown. Detection only:
-- there is deliberately NO command, trigger or actuation table in this schema.
--
-- Access model
--   * Devices write ONLY through the "ingest" edge function (service role).
--   * Portal users read through RLS, scoped to tenants in public.memberships.
--   * Roles: viewer (read), operator (read + acknowledge), admin (+ manage contacts).
--   * device_secrets has RLS enabled and NO policies: unreadable except by service role.

create extension if not exists pgcrypto;

create table public.tenants (
  id          uuid primary key default gen_random_uuid(),
  slug        text not null unique check (slug ~ '^[a-z0-9-]{2,48}$'),
  name        text not null,
  created_at  timestamptz not null default now()
);

create table public.sites (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references public.tenants(id) on delete cascade,
  slug        text not null check (slug ~ '^[a-z0-9-]{2,48}$'),
  name        text not null,
  timezone    text not null default 'Australia/Melbourne',
  created_at  timestamptz not null default now(),
  unique (tenant_id, slug)
);

create table public.devices (
  id              text primary key check (id ~ '^[A-Za-z0-9_-]{1,64}$'),
  tenant_id       uuid not null references public.tenants(id) on delete cascade,
  site_id         uuid not null references public.sites(id) on delete cascade,
  label           text not null,
  kind            text not null check (kind in ('foyer_optex', 'lg_boom_camera')),
  enabled         boolean not null default true,
  last_seen_at    timestamptz,
  last_heartbeat  jsonb,
  stale_alerted   boolean not null default false,
  created_at      timestamptz not null default now()
);
create index devices_site_idx on public.devices(site_id);

create table public.device_secrets (
  device_id   text primary key references public.devices(id) on delete cascade,
  secret      text not null check (length(secret) >= 32),
  rotated_at  timestamptz not null default now()
);

create table public.events (
  id            bigserial primary key,
  tenant_id     uuid not null references public.tenants(id) on delete cascade,
  site_id       uuid not null references public.sites(id) on delete cascade,
  device_id     text not null references public.devices(id) on delete cascade,
  seq           bigint not null check (seq > 0),
  event_type    text not null check (event_type in
                  ('TAILGATE','MULTIPLE','SENSOR_ERROR','PASS','DOOR_OPEN','BOOM_OVERTIME')),
  state         text not null check (state in ('ASSERT','CLEAR','PULSE')),
  input_name    text,
  occurred_at   timestamptz not null,
  received_at   timestamptz not null default now(),
  payload       jsonb not null default '{}'::jsonb,
  acknowledged_at timestamptz,
  acknowledged_by uuid,
  unique (device_id, seq)                       -- idempotent re-sends from the Pi
);
create index events_site_time_idx on public.events(site_id, occurred_at desc);
create index events_alert_idx on public.events(site_id, event_type, occurred_at desc)
  where event_type <> 'PASS';

create table public.alert_contacts (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references public.tenants(id) on delete cascade,
  site_id      uuid not null references public.sites(id) on delete cascade,
  name         text not null,
  email        text,
  phone_e164   text check (phone_e164 is null or phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  event_types  text[] not null default array['TAILGATE','MULTIPLE','BOOM_OVERTIME'],
  enabled      boolean not null default true,
  created_at   timestamptz not null default now(),
  check (email is not null or phone_e164 is not null)
);

create table public.alert_log (
  id          bigserial primary key,
  tenant_id   uuid not null references public.tenants(id) on delete cascade,
  event_id    bigint references public.events(id) on delete cascade,
  device_id   text references public.devices(id) on delete cascade,
  contact_id  uuid references public.alert_contacts(id) on delete set null,
  channel     text not null check (channel in ('email','sms')),
  kind        text not null check (kind in ('event','device_offline','device_recovered')),
  status      text not null check (status in ('sent','failed','not_configured')),
  detail      text,
  created_at  timestamptz not null default now()
);

create table public.memberships (
  user_id    uuid not null references auth.users(id) on delete cascade,
  tenant_id  uuid not null references public.tenants(id) on delete cascade,
  role       text not null check (role in ('viewer','operator','admin')),
  created_at timestamptz not null default now(),
  primary key (user_id, tenant_id)
);

-- ---------------------------------------------------------------- helpers --
-- SECURITY DEFINER so policies can consult memberships without recursive RLS.
create or replace function public.has_role(t uuid, min_role text)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.memberships m
    where m.user_id = auth.uid() and m.tenant_id = t
      and case min_role
            when 'viewer'   then m.role in ('viewer','operator','admin')
            when 'operator' then m.role in ('operator','admin')
            when 'admin'    then m.role = 'admin'
            else false end);
$$;
revoke all on function public.has_role(uuid, text) from public;
grant execute on function public.has_role(uuid, text) to authenticated;

-- Operators acknowledge alerts through this function only (cannot edit anything else).
create or replace function public.acknowledge_event(p_event_id bigint)
returns void language plpgsql security definer set search_path = public as $$
declare t uuid;
begin
  select tenant_id into t from public.events where id = p_event_id;
  if t is null or not public.has_role(t, 'operator') then
    raise exception 'not permitted' using errcode = '42501';
  end if;
  update public.events set acknowledged_at = now(), acknowledged_by = auth.uid()
   where id = p_event_id and acknowledged_at is null;
end $$;
revoke all on function public.acknowledge_event(bigint) from public;
grant execute on function public.acknowledge_event(bigint) to authenticated;

-- ---------------------------------------------------------------- RLS -------
alter table public.tenants        enable row level security;
alter table public.sites          enable row level security;
alter table public.devices        enable row level security;
alter table public.device_secrets enable row level security;   -- no policies: service role only
alter table public.events         enable row level security;
alter table public.alert_contacts enable row level security;
alter table public.alert_log      enable row level security;
alter table public.memberships    enable row level security;

create policy tenants_read  on public.tenants  for select to authenticated using (public.has_role(id, 'viewer'));
create policy sites_read    on public.sites    for select to authenticated using (public.has_role(tenant_id, 'viewer'));
create policy devices_read  on public.devices  for select to authenticated using (public.has_role(tenant_id, 'viewer'));
create policy events_read   on public.events   for select to authenticated using (public.has_role(tenant_id, 'viewer'));
create policy alertlog_read on public.alert_log for select to authenticated using (public.has_role(tenant_id, 'viewer'));
create policy members_read_own on public.memberships for select to authenticated
  using (user_id = auth.uid() or public.has_role(tenant_id, 'admin'));

create policy contacts_read   on public.alert_contacts for select to authenticated using (public.has_role(tenant_id, 'viewer'));
create policy contacts_insert on public.alert_contacts for insert to authenticated with check (public.has_role(tenant_id, 'admin'));
create policy contacts_update on public.alert_contacts for update to authenticated
  using (public.has_role(tenant_id, 'admin')) with check (public.has_role(tenant_id, 'admin'));
create policy contacts_delete on public.alert_contacts for delete to authenticated using (public.has_role(tenant_id, 'admin'));

-- No insert/update/delete policies on events, devices, sites, tenants, alert_log,
-- memberships: those are written by the service role (edge functions / RJL admin) only.

-- Column-level: stop the anon role reading anything at all.
revoke all on all tables in schema public from anon;
