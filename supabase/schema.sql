-- GST Foresight — Supabase schema
-- Run this once in the Supabase SQL editor before first ingest.

-- Enable pgvector extension
create extension if not exists vector;

-- ── chunks table ─────────────────────────────────────────────────────────────
-- Stores embedded document chunks from the ingest pipeline.
-- Written by Python (service_role key), read by Edge Function (anon key via RPC).

create table if not exists chunks (
  id           text primary key,          -- chunk_id from chunker.py
  doc_id       text not null,
  source_id    text not null,
  date         text,
  topic_tags   text,                      -- comma-separated
  chunk_index  int,
  content      text not null,
  embedding    vector(384),               -- all-MiniLM-L6-v2 dimensions
  inserted_at  timestamptz default now()
);

-- HNSW index for fast cosine similarity search
create index if not exists chunks_embedding_idx
  on chunks using hnsw (embedding vector_cosine_ops)
  with (m = 16, ef_construction = 64);

-- ── usage table ──────────────────────────────────────────────────────────────
-- Tracks query counts per IP for free-tier rate limiting.
-- One row per IP, reset_at marks when the 30-day window expires.

create table if not exists usage (
  ip           text primary key,
  query_count  int not null default 0,
  reset_at     timestamptz not null default (now() + interval '30 days')
);

-- ── match_chunks RPC ─────────────────────────────────────────────────────────
-- Called by the Edge Function to run semantic search.
-- Returns top-k chunks ordered by cosine similarity.

create or replace function match_chunks (
  query_embedding  vector(384),
  match_count      int     default 8,
  match_threshold  float   default 0.3
)
returns table (
  id          text,
  doc_id      text,
  source_id   text,
  date        text,
  topic_tags  text,
  content     text,
  similarity  float
)
language sql stable
security definer
set search_path = public, extensions
as $$
  select
    id,
    doc_id,
    source_id,
    date,
    topic_tags,
    content,
    1 - (embedding <=> query_embedding) as similarity
  from chunks
  where 1 - (embedding <=> query_embedding) >= match_threshold
  order by embedding <=> query_embedding
  limit match_count;
$$;

-- ── check_and_increment_usage RPC ────────────────────────────────────────────
-- Atomically checks rate limit and increments counter.
-- Returns { allowed: bool, remaining: int } so the Edge Function
-- makes a single DB call instead of two.
--
-- SECURITY DEFINER: runs with the function owner's privileges (service_role),
-- not the caller's. This means anon users can call this RPC but cannot
-- directly read or modify the usage table — all writes go through this
-- function which enforces the rate-limit logic.

create or replace function check_and_increment_usage (
  client_ip   text,
  free_limit  int default 5
)
returns json
language plpgsql
security definer
set search_path = public
as $$
declare
  rec        usage%rowtype;
begin
  -- Ensure the row exists before we lock it.
  -- ON CONFLICT DO NOTHING means only the first concurrent INSERT wins;
  -- all others skip silently, then proceed to the SELECT FOR UPDATE below.
  insert into usage (ip, query_count, reset_at)
  values (client_ip, 0, now() + interval '30 days')
  on conflict (ip) do nothing;

  -- Row-level lock: the second of two concurrent requests blocks here
  -- until the first commits, so only one request at a time executes the
  -- check-and-increment logic. This closes the TOCTOU race completely.
  select * into rec from usage where ip = client_ip for update;

  -- Reset window if expired before checking limit
  if rec.reset_at < now() then
    update usage
    set query_count = 1, reset_at = now() + interval '30 days'
    where ip = client_ip
    returning * into rec;
    return json_build_object(
      'allowed',   true,
      'remaining', free_limit - 1,
      'reset_at',  rec.reset_at
    );
  end if;

  -- Blocked: already at or over limit
  if rec.query_count >= free_limit then
    return json_build_object(
      'allowed',   false,
      'remaining', 0,
      'reset_at',  rec.reset_at
    );
  end if;

  -- Allowed: atomically increment
  update usage set query_count = query_count + 1 where ip = client_ip;
  return json_build_object(
    'allowed',   true,
    'remaining', free_limit - rec.query_count - 1,
    'reset_at',  rec.reset_at
  );
end;
$$;

-- ── Row-level security ────────────────────────────────────────────────────────
-- chunks: no direct REST access for anon. All reads go through match_chunks RPC
-- which runs as SECURITY DEFINER (owner privileges, bypasses RLS). This prevents
-- bulk corpus extraction via GET /rest/v1/chunks?select=*.
alter table chunks enable row level security;
-- No anon select policy — direct REST reads are blocked.
-- match_chunks is SECURITY DEFINER so it reads chunks as the function owner,
-- not as the calling anon role, and therefore bypasses this RLS restriction.
create policy "service_role full access on chunks"
  on chunks for all to service_role using (true) with check (true);

-- usage: no direct anon access — all reads/writes go through the
-- check_and_increment_usage SECURITY DEFINER function above.
-- Direct REST access to /rest/v1/usage is blocked for anon users,
-- preventing rate-limit bypass (reset own counter) or DoS (exhaust others).
alter table usage enable row level security;
create policy "service_role only on usage"
  on usage for all to service_role using (true) with check (true);

-- ═════════════════════════════════════════════════════════════════════════════
-- PHASE 3 — Auth, history, alerts, subscriptions, teams
-- Run in the Supabase SQL editor after Phase 1/2 schema is applied.
-- Requires: Supabase Auth enabled in the project dashboard.
-- ═════════════════════════════════════════════════════════════════════════════

-- ── profiles ─────────────────────────────────────────────────────────────────
-- One row per user, auto-created by trigger on auth.users insert.
-- display_name is pulled from OAuth metadata (Google full_name) or set manually.

create table if not exists profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  display_name  text,
  created_at    timestamptz default now()
);

-- Auto-create a profile row whenever a new user signs up (email or OAuth).
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    coalesce(
      new.raw_user_meta_data ->> 'full_name',
      new.raw_user_meta_data ->> 'name',
      split_part(new.email, '@', 1)   -- fallback: username part of email
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create or replace trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure handle_new_user();

-- ── subscriptions ─────────────────────────────────────────────────────────────
-- Tracks individual Pro subscriptions. Firm subscriptions are tracked on teams.
-- Written by api/activate.js (service_role) after Razorpay payment confirmation.
-- Never written by the client directly.

create table if not exists subscriptions (
  id                   uuid primary key default gen_random_uuid(),
  user_id              uuid not null references auth.users(id) on delete cascade,
  plan                 text not null check (plan in ('pro_individual', 'pro_firm')),
  valid_until          timestamptz not null,
  razorpay_order_id    text,
  razorpay_payment_id  text unique,       -- unique prevents double-activation
  created_at           timestamptz default now()
);

create index if not exists subscriptions_user_id_idx on subscriptions (user_id);

-- ── teams ─────────────────────────────────────────────────────────────────────
-- A team is created by a firm buyer. Up to max_seats members can be added.
-- valid_until comes from the firm Pro subscription payment.

create table if not exists teams (
  id           uuid primary key default gen_random_uuid(),
  name         text not null,
  owner_id     uuid not null references auth.users(id),
  valid_until  timestamptz,               -- null = no active subscription
  max_seats    int not null default 5,
  created_at   timestamptz default now()
);

-- ── team_members ──────────────────────────────────────────────────────────────
-- Many-to-many: users ↔ teams. Owner always has role='owner'.
-- Seat count enforced by check_seat_limit trigger below.

create table if not exists team_members (
  team_id    uuid not null references teams(id) on delete cascade,
  user_id    uuid not null references auth.users(id) on delete cascade,
  role       text not null default 'member' check (role in ('owner', 'member')),
  joined_at  timestamptz default now(),
  primary key (team_id, user_id)
);

create index if not exists team_members_user_id_idx on team_members (user_id);

-- Prevent adding members beyond max_seats.
create or replace function check_seat_limit()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  current_seats int;
  max_seats     int;
begin
  select count(*), t.max_seats
  into current_seats, max_seats
  from team_members tm
  join teams t on t.id = tm.team_id
  where tm.team_id = new.team_id
  group by t.max_seats;

  if current_seats >= max_seats then
    raise exception 'Team seat limit reached (max %)', max_seats;
  end if;
  return new;
end;
$$;

create or replace trigger enforce_seat_limit
  before insert on team_members
  for each row execute procedure check_seat_limit();

-- ── query_history ─────────────────────────────────────────────────────────────
-- One row per answered query for logged-in users. Anonymous queries are not saved.
-- sources stored as JSONB array: [{source_id, date, topic_tags, excerpt}]

create table if not exists query_history (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  query       text not null,
  answer      text,
  sources     jsonb,
  created_at  timestamptz default now()
);

create index if not exists query_history_user_id_idx
  on query_history (user_id, created_at desc);

-- ── alert_subscriptions ───────────────────────────────────────────────────────
-- Users subscribe to specific topic IDs from the 12-topic taxonomy.
-- topic_id matches the prediction engine's topic keys (e.g. 'itc_eligibility').
-- threshold_delta: notify when a topic's probability shifts by ≥ this many points
-- between consecutive latest.json generations.
-- The GitHub Actions alerts workflow reads this table (service_role) and sends
-- email via Resend. email column is denormalised for simpler workflow queries.

create table if not exists alert_subscriptions (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references auth.users(id) on delete cascade,
  topic_id         text not null,
  threshold_delta  int not null default 10 check (threshold_delta between 1 and 100),
  email            text not null,
  active           boolean not null default true,
  created_at       timestamptz default now(),
  unique (user_id, topic_id)   -- one subscription per topic per user; use UPDATE to change threshold
);

create index if not exists alert_subscriptions_topic_id_idx
  on alert_subscriptions (topic_id) where active = true;

-- ═════════════════════════════════════════════════════════════════════════════
-- PHASE 3 — RPCs
-- ═════════════════════════════════════════════════════════════════════════════

-- ── is_pro ────────────────────────────────────────────────────────────────────
-- Single call from api/query.js to check whether a user has Pro access,
-- either via an individual subscription or a team membership.
-- Returns true/false — caller decides whether to skip IP rate limiting.
-- SECURITY DEFINER: readable by anon/authenticated with just the anon key.

create or replace function is_pro (p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  -- Individual Pro subscription still valid
  select exists (
    select 1 from subscriptions
    where user_id = p_user_id
      and valid_until > now()
  )
  or
  -- Member of a team with an active firm subscription
  exists (
    select 1
    from team_members tm
    join teams t on t.id = tm.team_id
    where tm.user_id = p_user_id
      and t.valid_until > now()
  );
$$;

-- ── save_query ────────────────────────────────────────────────────────────────
-- Called by api/query.js after a successful answer to persist history.
-- SECURITY DEFINER so the edge function can write with the anon key
-- (the service key is not exposed to the Vercel edge runtime).
-- p_sources: JSON array string — parsed to jsonb inside the function.

create or replace function save_query (
  p_user_id  uuid,
  p_query    text,
  p_answer   text,
  p_sources  text   -- JSON array string from the edge function
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  new_id uuid;
begin
  insert into query_history (user_id, query, answer, sources)
  values (p_user_id, p_query, p_answer, p_sources::jsonb)
  returning id into new_id;
  return new_id;
end;
$$;

-- ── get_history ───────────────────────────────────────────────────────────────
-- Returns the 50 most recent queries for a user.
-- SECURITY DEFINER: callable with anon key; enforces user_id = caller only
-- by comparing p_user_id to auth.uid() — rejects mismatched requests.

create or replace function get_history (p_user_id uuid)
returns table (
  id          uuid,
  query       text,
  answer      text,
  sources     jsonb,
  created_at  timestamptz
)
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  -- Callers may only fetch their own history.
  -- auth.uid() is set by Supabase from the JWT in the Authorization header.
  if auth.uid() != p_user_id then
    raise exception 'Access denied';
  end if;

  return query
    select h.id, h.query, h.answer, h.sources, h.created_at
    from query_history h
    where h.user_id = p_user_id
    order by h.created_at desc
    limit 50;
end;
$$;

-- ═════════════════════════════════════════════════════════════════════════════
-- PHASE 3 — Row-level security
-- ═════════════════════════════════════════════════════════════════════════════

-- profiles: users read/update only their own row.
alter table profiles enable row level security;
create policy "users manage own profile"
  on profiles for all to authenticated
  using (id = auth.uid()) with check (id = auth.uid());
create policy "service_role full access on profiles"
  on profiles for all to service_role using (true) with check (true);

-- subscriptions: users read their own; writes only via service_role (api/activate.js).
alter table subscriptions enable row level security;
create policy "users read own subscriptions"
  on subscriptions for select to authenticated
  using (user_id = auth.uid());
create policy "service_role full access on subscriptions"
  on subscriptions for all to service_role using (true) with check (true);

-- teams: owner manages all; members read only.
alter table teams enable row level security;
create policy "owner manages team"
  on teams for all to authenticated
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy "members read own team"
  on teams for select to authenticated
  using (
    exists (
      select 1 from team_members
      where team_id = teams.id and user_id = auth.uid()
    )
  );
create policy "service_role full access on teams"
  on teams for all to service_role using (true) with check (true);

-- team_members: members see their own row; owner sees all rows in their team.
alter table team_members enable row level security;
create policy "members read own membership"
  on team_members for select to authenticated
  using (user_id = auth.uid());
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
create policy "service_role full access on team_members"
  on team_members for all to service_role using (true) with check (true);

-- query_history: users see and insert only their own rows.
-- Deletes not allowed — history is append-only from the user's perspective.
alter table query_history enable row level security;
create policy "users manage own history"
  on query_history for select to authenticated
  using (user_id = auth.uid());
create policy "service_role full access on query_history"
  on query_history for all to service_role using (true) with check (true);

-- alert_subscriptions: users manage their own subscriptions.
alter table alert_subscriptions enable row level security;
create policy "users manage own alerts"
  on alert_subscriptions for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "service_role full access on alert_subscriptions"
  on alert_subscriptions for all to service_role using (true) with check (true);

-- ═════════════════════════════════════════════════════════════════════════════
-- PHASE 4 — Query Personalization
-- Run after Phase 3 schema is applied.
-- Adds topic_tags to query_history, new get_recent_topics RPC, and updates
-- save_query to (a) persist topic tags and (b) enforce ownership via auth.uid().
-- api/query.js forwards the user's JWT for save_query and get_recent_topics so
-- auth.uid() resolves correctly inside these SECURITY DEFINER functions.
-- ═════════════════════════════════════════════════════════════════════════════

-- ── query_history: add topic_tags column ─────────────────────────────────────
alter table query_history
  add column if not exists topic_tags text;  -- comma-separated, same format as chunks.topic_tags

-- Covering index for the hot-path: get last N topic_tags for a user without
-- reading answer/sources columns.
create index if not exists query_history_user_recent_idx
  on query_history (user_id, created_at desc) include (topic_tags);

-- ── save_query — updated: adds p_topic_tags, adds ownership check ─────────────
-- Drop the old 4-param version before creating the new 5-param signature.
-- The new version requires the caller to use their own user JWT (not anon key)
-- so that auth.uid() resolves and the ownership check passes.
drop function if exists save_query(uuid, text, text, text);

create or replace function save_query (
  p_user_id    uuid,
  p_query      text,
  p_answer     text,
  p_sources    text,
  p_topic_tags text default null
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  new_id uuid;
begin
  -- Callers must use their own JWT. auth.uid() is set by PostgREST from the
  -- Bearer token — this prevents any caller from writing into another user's history.
  if auth.uid() != p_user_id then
    raise exception 'Access denied';
  end if;

  insert into query_history (user_id, query, answer, sources, topic_tags)
  values (p_user_id, p_query, p_answer, p_sources::jsonb, p_topic_tags)
  returning id into new_id;
  return new_id;
end;
$$;

-- Restrict save_query: callable only by authenticated users and service_role.
-- Anon callers (who only have the public anon key) are blocked.
revoke execute on function save_query(uuid, text, text, text, text) from public;
grant execute on function save_query(uuid, text, text, text, text) to authenticated;
grant execute on function save_query(uuid, text, text, text, text) to service_role;

-- ── get_recent_topics — new RPC ───────────────────────────────────────────────
-- Called by api/query.js with the user's JWT to fetch the last N query topics.
-- Returns only topic_tags + query text (not full answers) for minimal payload.
-- Ownership enforced via auth.uid() = p_user_id — same pattern as get_history.

create or replace function get_recent_topics (p_user_id uuid, p_limit int default 3)
returns table (topic_tags text, query text, created_at timestamptz)
language sql
stable
security definer
set search_path = public, extensions
as $$
  select h.topic_tags, h.query, h.created_at
  from query_history h
  where h.user_id = p_user_id
    and auth.uid() = p_user_id
    and h.topic_tags is not null
    and h.topic_tags != ''
  order by h.created_at desc
  limit p_limit;
$$;

-- Restrict get_recent_topics: callable only by authenticated users.
revoke execute on function get_recent_topics(uuid, int) from public;
grant execute on function get_recent_topics(uuid, int) to authenticated;
