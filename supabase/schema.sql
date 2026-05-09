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
  query_embedding vector(384),
  match_count     int default 8
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
  order by embedding <=> query_embedding
  limit match_count;
$$;

-- ── check_and_increment_usage RPC ────────────────────────────────────────────
-- Atomically checks rate limit and increments counter.
-- Returns { allowed: bool, remaining: int } so the Edge Function
-- makes a single DB call instead of two.

create or replace function check_and_increment_usage (
  client_ip   text,
  free_limit  int default 5
)
returns json
language plpgsql
as $$
declare
  rec        usage%rowtype;
  remaining  int;
begin
  -- Upsert row, reset window if expired
  insert into usage (ip, query_count, reset_at)
  values (client_ip, 0, now() + interval '30 days')
  on conflict (ip) do update
    set query_count = case
          when usage.reset_at < now() then 0
          else usage.query_count
        end,
        reset_at = case
          when usage.reset_at < now() then now() + interval '30 days'
          else usage.reset_at
        end;

  select * into rec from usage where ip = client_ip;

  if rec.query_count >= free_limit then
    return json_build_object('allowed', false, 'remaining', 0, 'reset_at', rec.reset_at);
  end if;

  update usage set query_count = query_count + 1 where ip = client_ip;
  remaining := free_limit - rec.query_count - 1;

  return json_build_object('allowed', true, 'remaining', remaining, 'reset_at', rec.reset_at);
end;
$$;

-- ── Row-level security ────────────────────────────────────────────────────────
-- chunks: readable by anon (via RPC only), writable by service_role only
alter table chunks enable row level security;
create policy "anon can read chunks via rpc"
  on chunks for select to anon using (true);

-- usage: anon can upsert their own row only
alter table usage enable row level security;
create policy "anon can manage own usage row"
  on usage for all to anon using (true) with check (true);
