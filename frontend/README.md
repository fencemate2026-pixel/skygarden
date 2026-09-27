# Front end: MagicKeys portal, re-skinned for Detect

Decision (Jarrod, 27 Sep 2026): same MagicKeys front end as Balwyn, different images, this repo's backend.
The Balwyn v53 source is not in this repo yet. Import it here, then make these changes before any client login:

1. Remove every gate/door control: open buttons, command queue, door bridge. Detect is read-only.
2. Carry over the role fix (`magickeys-role-and-privacy`): v52/v53 make every sign-in an administrator.
   Roles here come from `public.memberships`.
3. Replace the hardcoded gate register with data from `sites` and `devices`.
4. Screens: alert feed (with acknowledge for operators), daily entry counts from `PASS`, unit health from
   `devices.last_heartbeat` and `last_seen_at`, alert contacts (admins), monthly report.
5. Sky Garden imagery and branding, co-branded RJL + MagicKeys.
Reads go through the Supabase anon key + user JWT, so RLS is the only thing between tenants: never use the
service role key in the browser.
