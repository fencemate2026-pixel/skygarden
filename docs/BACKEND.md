# Backend deploy (new Supabase project "magickeys-detect")

Separate project from Balwyn and Thomastown (MagicKeys `ifesjmdhlyurswgajslm`). Nothing here touches either.

1. Create the project (Sydney region). Apply `supabase/migrations/`.
2. Deploy functions (devices sign requests themselves, so JWT verification is off for these two):
   ```
   supabase functions deploy ingest --no-verify-jwt
   supabase functions deploy stale-check --no-verify-jwt
   supabase secrets set CRON_SECRET=<48+ random chars> STALE_MINUTES=10
   supabase secrets set RESEND_API_KEY=... ALERT_FROM_EMAIL=alerts@<verified domain>
   supabase secrets set TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_FROM=...
   ```
   Providers are optional; missing ones are logged as `not_configured`.
3. Schedule stale-check every 5 minutes (SQL editor; needs pg_cron + pg_net enabled; store CRON_SECRET in Vault):
   ```sql
   select vault.create_secret('<CRON_SECRET>', 'mkd_cron_secret');
   select cron.schedule('mkd-stale-check', '*/5 * * * *', $$
     select net.http_post(
       url := 'https://<project-ref>.supabase.co/functions/v1/stale-check',
       headers := jsonb_build_object('Authorization', 'Bearer ' ||
         (select decrypted_secret from vault.decrypted_secrets where name = 'mkd_cron_secret')));
   $$);
   ```
4. Provision a tenant, site, device and its secret (service role / SQL editor):
   ```sql
   insert into tenants (slug, name) values ('sky-garden', 'Sky Garden') returning id;
   insert into sites (tenant_id, slug, name) values ('<tenant id>', 'main', 'Sky Garden') returning id;
   insert into devices (id, tenant_id, site_id, label, kind)
     values ('skygarden-foyer-01', '<tenant id>', '<site id>', 'Main foyer', 'foyer_optex');
   insert into device_secrets (device_id, secret)
     values ('skygarden-foyer-01', encode(gen_random_bytes(32), 'hex')) returning secret;  -- copy to the Pi's agent.env, once
   insert into alert_contacts (tenant_id, site_id, name, email, event_types)
     values ('<tenant id>', '<site id>', 'RJL ops', 'info@rjlcommercialgroup.com', array['SENSOR_ERROR','DEVICE_OFFLINE']);
   ```
   Building contacts get `array['TAILGATE','MULTIPLE','BOOM_OVERTIME']` (the default).
5. Portal users: create in Supabase Auth, then `insert into memberships (user_id, tenant_id, role)`.
   Roles: viewer (read), operator (read + acknowledge), admin (+ manage alert contacts).

Known limits: no per-device rate limiting on ingest yet (unknown device IDs cost one DB lookup each);
a device that has never reported is not flagged by stale-check (commissioning covers first contact).
