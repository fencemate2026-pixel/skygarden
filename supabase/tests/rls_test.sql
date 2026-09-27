-- RLS / isolation tests. Run with run_sql_tests.sh. Any failed assert aborts (ON_ERROR_STOP).
grant select on all tables in schema public to authenticated;
grant insert, update, delete on public.alert_contacts to authenticated;
grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role, authenticated;

-- fixtures (as superuser = service role equivalent)
insert into auth.users values
 ('11111111-1111-1111-1111-111111111111'),  -- Sky Garden viewer
 ('22222222-2222-2222-2222-222222222222'),  -- other tenant admin
 ('33333333-3333-3333-3333-333333333333');  -- Sky Garden operator
insert into public.tenants (id, slug, name) values
 ('aaaaaaaa-0000-0000-0000-000000000001','sky-garden','Sky Garden'),
 ('bbbbbbbb-0000-0000-0000-000000000002','other-oc','Other OC');
insert into public.sites (id, tenant_id, slug, name) values
 ('aaaaaaaa-0000-0000-0000-0000000000a1','aaaaaaaa-0000-0000-0000-000000000001','main','Sky Garden'),
 ('bbbbbbbb-0000-0000-0000-0000000000b1','bbbbbbbb-0000-0000-0000-000000000002','main','Other');
insert into public.devices (id, tenant_id, site_id, label, kind) values
 ('skygarden-foyer-01','aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-0000000000a1','Foyer','foyer_optex'),
 ('other-foyer-01','bbbbbbbb-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-0000000000b1','Foyer','foyer_optex');
insert into public.device_secrets values ('skygarden-foyer-01', repeat('s',40), now());
insert into public.events (tenant_id, site_id, device_id, seq, event_type, state, occurred_at) values
 ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-0000000000a1','skygarden-foyer-01',1,'TAILGATE','ASSERT',now()),
 ('bbbbbbbb-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-0000000000b1','other-foyer-01',1,'TAILGATE','ASSERT',now());
insert into public.memberships values
 ('11111111-1111-1111-1111-111111111111','aaaaaaaa-0000-0000-0000-000000000001','viewer',now()),
 ('22222222-2222-2222-2222-222222222222','bbbbbbbb-0000-0000-0000-000000000002','admin',now()),
 ('33333333-3333-3333-3333-333333333333','aaaaaaaa-0000-0000-0000-000000000001','operator',now());

-- T1: idempotency - same (device, seq) cannot be stored twice
do $$ begin
  insert into public.events (tenant_id, site_id, device_id, seq, event_type, state, occurred_at) values
   ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-0000000000a1','skygarden-foyer-01',1,'PASS','PULSE',now());
  raise exception 'T1 FAIL: duplicate seq accepted';
exception when unique_violation then raise notice 'T1 pass: duplicate seq rejected';
end $$;

-- T2: viewer sees only own tenant
set role authenticated;
select set_config('request.jwt.claim.sub','11111111-1111-1111-1111-111111111111',false);
do $$ declare n int; o int; begin
  select count(*) into n from public.events;
  select count(*) into o from public.events where device_id='other-foyer-01';
  if n <> 1 or o <> 0 then raise exception 'T2 FAIL: viewer saw % events (% foreign)', n, o; end if;
  raise notice 'T2 pass: viewer isolated to own tenant';
end $$;

-- T3: device_secrets unreadable to any portal user
do $$ declare n int; begin
  select count(*) into n from public.device_secrets;
  if n <> 0 then raise exception 'T3 FAIL: secrets visible'; end if;
  raise notice 'T3 pass: device secrets hidden';
end $$;

-- T4: viewer cannot write events or contacts, cannot acknowledge
do $$ begin
  begin
    insert into public.events (tenant_id, site_id, device_id, seq, event_type, state, occurred_at) values
     ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-0000000000a1','skygarden-foyer-01',99,'PASS','PULSE',now());
    raise exception 'T4 FAIL: viewer inserted event';
  exception when insufficient_privilege then null; end;
  begin
    insert into public.alert_contacts (tenant_id, site_id, name, email) values
     ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-0000000000a1','x','x@example.com');
    raise exception 'T4 FAIL: viewer inserted contact';
  exception when insufficient_privilege then null; end;
  begin
    perform public.acknowledge_event((select id from public.events limit 1));
    raise exception 'T4 FAIL: viewer acknowledged';
  exception when insufficient_privilege then null; end;
  raise notice 'T4 pass: viewer is read-only';
end $$;

-- T5: operator can acknowledge own tenant, not the other tenant
select set_config('request.jwt.claim.sub','33333333-3333-3333-3333-333333333333',false);
do $$ declare own bigint; begin
  select id into own from public.events limit 1;
  perform public.acknowledge_event(own);
  if (select acknowledged_at from public.events where id = own) is null then
    raise exception 'T5 FAIL: ack not recorded'; end if;
  raise notice 'T5 pass: operator acknowledged own event';
end $$;
reset role;
do $$ declare foreign_id bigint; begin
  select id into foreign_id from public.events where device_id='other-foyer-01';
  perform set_config('request.jwt.claim.sub','33333333-3333-3333-3333-333333333333',false);
  execute 'set role authenticated';
  begin
    perform public.acknowledge_event(foreign_id);
    raise exception 'T5b FAIL: cross-tenant ack';
  exception when insufficient_privilege then raise notice 'T5b pass: cross-tenant ack refused'; end;
  execute 'reset role';
end $$;

-- T6: other tenant admin manages own contacts but cannot touch Sky Garden's
set role authenticated;
select set_config('request.jwt.claim.sub','22222222-2222-2222-2222-222222222222',false);
do $$ begin
  insert into public.alert_contacts (tenant_id, site_id, name, email) values
   ('bbbbbbbb-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-0000000000b1','OC mgr','m@example.com');
  begin
    insert into public.alert_contacts (tenant_id, site_id, name, email) values
     ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-0000000000a1','intruder','i@example.com');
    raise exception 'T6 FAIL: cross-tenant contact insert';
  exception when insufficient_privilege then null; end;
  raise notice 'T6 pass: admin limited to own tenant';
end $$;

-- T7: anonymous role reads nothing
reset role;
set role anon;
do $$ begin
  perform 1 from public.events;
  raise exception 'T7 FAIL: anon read events';
exception when insufficient_privilege then raise notice 'T7 pass: anon denied';
end $$;
reset role;

-- T8: no actuation tables exist in this schema
do $$ declare n int; begin
  select count(*) into n from information_schema.tables
   where table_schema='public' and table_name ~* '(trigger|command|relay|pulse|unlock|actuat)';
  if n <> 0 then raise exception 'T8 FAIL: actuation-like table present'; end if;
  raise notice 'T8 pass: no actuation tables';
end $$;
