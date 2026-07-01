-- fix_missing_rls_policies
--
-- ROOT CAUSE 1 (P0): production `usage` table carried a policy named
-- "anon can manage own usage row" — `for all to anon using (true) with check (true)`.
-- This is NOT what supabase/schema.sql specifies (service_role-only). It let any
-- caller holding the public anon key read/update/delete ANY row in `usage` directly
-- via PostgREST (/rest/v1/usage), bypassing check_and_increment_usage entirely and
-- defeating the 5-free-queries/month rate limit. Confirmed live: an unauthenticated
-- GET to /rest/v1/usage?select=* returned all IP rows with their query_count/reset_at.
--
-- ROOT CAUSE 2 (P1): Phase 3 tables (profiles, subscriptions, teams, team_members,
-- query_history, alert_subscriptions) have RLS enabled in production but had ZERO
-- policies attached (confirmed via pg_policies), even though schema.sql defines
-- policies for all of them. RLS enabled + no policy = deny-all for any role without
-- BYPASSRLS. This silently broke api/subscribe.js's direct REST writes to
-- alert_subscriptions (every POST/DELETE would fail with a DB permission error,
-- surfaced to the client as a generic 502 db_error) — the "Subscribe to alerts"
-- feature has never worked in production. It also blocked any legitimate
-- authenticated-role SELECT on subscriptions/profiles/query_history.
--
-- This migration re-applies the exact policies already reviewed and documented in
-- supabase/schema.sql, restoring parity between the tracked schema and prod state.

-- ── usage: revoke the accidental anon-writable policy, restore service_role-only ──
drop policy if exists "anon can manage own usage row" on usage;
drop policy if exists "service_role only on usage" on usage;
create policy "service_role only on usage"
  on usage for all to service_role using (true) with check (true);

-- ── profiles ─────────────────────────────────────────────────────────────────
drop policy if exists "users manage own profile" on profiles;
create policy "users manage own profile"
  on profiles for all to authenticated
  using (id = auth.uid()) with check (id = auth.uid());
drop policy if exists "service_role full access on profiles" on profiles;
create policy "service_role full access on profiles"
  on profiles for all to service_role using (true) with check (true);

-- ── subscriptions ────────────────────────────────────────────────────────────
drop policy if exists "users read own subscriptions" on subscriptions;
create policy "users read own subscriptions"
  on subscriptions for select to authenticated
  using (user_id = auth.uid());
drop policy if exists "service_role full access on subscriptions" on subscriptions;
create policy "service_role full access on subscriptions"
  on subscriptions for all to service_role using (true) with check (true);

-- ── teams ────────────────────────────────────────────────────────────────────
drop policy if exists "owner manages team" on teams;
create policy "owner manages team"
  on teams for all to authenticated
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
drop policy if exists "members read own team" on teams;
create policy "members read own team"
  on teams for select to authenticated
  using (
    exists (
      select 1 from team_members
      where team_id = teams.id and user_id = auth.uid()
    )
  );
drop policy if exists "service_role full access on teams" on teams;
create policy "service_role full access on teams"
  on teams for all to service_role using (true) with check (true);

-- ── team_members ─────────────────────────────────────────────────────────────
drop policy if exists "members read own membership" on team_members;
create policy "members read own membership"
  on team_members for select to authenticated
  using (user_id = auth.uid());
drop policy if exists "owner manages team members" on team_members;
create policy "owner manages team members"
  on team_members for all to authenticated
  using (
    exists (
      select 1 from teams
      where id = team_members.team_id and owner_id = auth.uid()
    )
  )
  with check (
    exists (
      select 1 from teams
      where id = team_members.team_id and owner_id = auth.uid()
    )
  );
drop policy if exists "service_role full access on team_members" on team_members;
create policy "service_role full access on team_members"
  on team_members for all to service_role using (true) with check (true);

-- ── query_history ────────────────────────────────────────────────────────────
drop policy if exists "users manage own history" on query_history;
create policy "users manage own history"
  on query_history for select to authenticated
  using (user_id = auth.uid());
drop policy if exists "service_role full access on query_history" on query_history;
create policy "service_role full access on query_history"
  on query_history for all to service_role using (true) with check (true);

-- ── alert_subscriptions ──────────────────────────────────────────────────────
drop policy if exists "users manage own alerts" on alert_subscriptions;
create policy "users manage own alerts"
  on alert_subscriptions for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists "service_role full access on alert_subscriptions" on alert_subscriptions;
create policy "service_role full access on alert_subscriptions"
  on alert_subscriptions for all to service_role using (true) with check (true);

-- ── cleanup: drop leftover pre-threshold match_chunks(vector, int) overload ────
-- SECURITY INVOKER, no search_path pinned (flagged by advisor as
-- function_search_path_mutable), no match_threshold filter. Confirmed unused —
-- api/query.js always calls the 3-arg (vector, int, float) SECURITY DEFINER
-- version. RLS on `chunks` already blocks anon SELECT so this stale overload
-- could not leak data, but it's dead code with a lint warning; remove it.
drop function if exists public.match_chunks(vector, integer);
