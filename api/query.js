/**
 * api/query.js — GST Foresight query endpoint (Vercel Edge Function)
 *
 * Flow:
 *   POST /api/query { query: string }
 *     → check_and_increment_usage RPC  (rate limit: 5 req / 30 days, by IP)
 *     → match_chunks RPC               (pgvector cosine search, top 8)
 *     → RAG prompt → Sarvam sarvam-m
 *     → { answer, sources, remaining_queries }
 *
 * Env vars required (set in Vercel dashboard):
 *   SUPABASE_URL          — https://xxxx.supabase.co
 *   SUPABASE_ANON_KEY     — public anon key (safe to use from edge)
 *   SARVAM_API_KEY        — api-subscription-key header value
 */

import { createClient } from '@supabase/supabase-js';
import { ipAddress } from '@vercel/functions';

export const config = { runtime: 'edge' };

// ─── Supabase client ──────────────────────────────────────────────────────────

function getSupabase() {
  return createClient(
    process.env.SUPABASE_URL,
    process.env.SUPABASE_ANON_KEY,
    { auth: { persistSession: false } },
  );
}

// ─── CORS ─────────────────────────────────────────────────────────────────────

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Content-Type': 'application/json',
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: CORS });
}

// ─── Prompt builder ───────────────────────────────────────────────────────────

function buildPrompt(query, chunks) {
  const context = chunks
    .map((c, i) => {
      const date = c.date ? ` (${c.date.slice(0, 10)})` : '';
      const topics = c.topic_tags ? ` [${c.topic_tags}]` : '';
      return `[${i + 1}] ${c.source_id}${date}${topics}\n${c.content}`;
    })
    .join('\n\n---\n\n');

  return `You are a GST regulatory foresight analyst for India.

Using ONLY the corpus excerpts below, answer the user's query with:
1. A probability assessment of whether the regulatory change is likely (low / medium / high)
2. The specific signals from the documents that drive this assessment
3. Expected timeframe (next council meeting / next budget / 2–3 quarters / next FY)
4. Concrete things the user should monitor or prepare for

Stay strictly grounded in the documents. If the corpus does not contain enough signal, say so clearly rather than speculating.

CORPUS EXCERPTS:
${context}

USER QUERY: ${query}

Respond in this format:
**Likelihood**: [Low / Medium / High] — [one-line reason]
**Timeframe**: [expected horizon]
**Key signals**:
- [signal 1 with source reference]
- [signal 2 with source reference]
**What to watch**: [specific monitoring advice]
**Confidence note**: [any caveats about data coverage]`;
}

// ─── Handler ──────────────────────────────────────────────────────────────────

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== 'POST') {
      return json({ error: 'method_not_allowed' }, 405);
    }

    // Parse body
    let query;
    try {
      ({ query } = await request.json());
    } catch {
      return json({ error: 'invalid_json' }, 400);
    }
    if (!query || typeof query !== 'string' || query.trim().length < 5) {
      return json({ error: 'query_too_short', message: 'Please enter a more specific question.' }, 400);
    }

    const cleanQuery = query.trim().slice(0, 500);
    const supabase = getSupabase();

    // ── Rate limit ────────────────────────────────────────────────────────────
    // Single atomic RPC: check window, reset if expired, increment, return result

    const ip = ipAddress(request) ?? '0.0.0.0';
    const { data: rl, error: rlErr } = await supabase.rpc('check_and_increment_usage', {
      client_ip: ip,
      free_limit: 5,
    });

    if (rlErr) {
      console.error('[supabase] rate limit error:', rlErr.message);
      // Fail open — don't block the user if rate limit DB is unreachable
    } else if (rl && !rl.allowed) {
      return json(
        {
          error: 'rate_limited',
          message: 'You have used all 5 free queries for this month.',
          reset_at: rl.reset_at,
        },
        429,
      );
    }

    // ── Embed query ───────────────────────────────────────────────────────────
    // Generate embedding for the query using the same model as the ingest pipeline.
    // We call a small Supabase Edge Function or use Supabase's built-in
    // text-embedding via the Transformers.js WASM route.
    //
    // Practical shortcut for V8 edge: call Supabase's /functions/v1/embed
    // OR use the Supabase built-in vector embedding feature (if configured).
    //
    // For now we use the Supabase REST approach — pass raw text to match_chunks
    // via a text-based RPC that handles embedding server-side using pg_trgm
    // as a fallback, or configure Supabase's built-in AI embedding.
    //
    // Simplest working approach: embed via a POST to Supabase's embedding edge
    // function (deployed alongside this project, see api/embed.js).

    let embedding;
    try {
      const embedRes = await fetch(`${process.env.SUPABASE_URL}/functions/v1/embed`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${process.env.SUPABASE_ANON_KEY}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ text: cleanQuery }),
      });
      if (!embedRes.ok) throw new Error(`embed status ${embedRes.status}`);
      ({ embedding } = await embedRes.json());
    } catch (e) {
      console.error('[embed] failed:', e);
      return json({ error: 'embed_error', message: 'Failed to process query.' }, 502);
    }

    // ── Vector search ─────────────────────────────────────────────────────────

    const { data: chunks, error: searchErr } = await supabase.rpc('match_chunks', {
      query_embedding: embedding,
      match_count: 8,
    });

    if (searchErr) {
      console.error('[supabase] vector search error:', searchErr.message);
      return json({ error: 'search_error', message: 'Vector search unavailable.' }, 502);
    }

    if (!chunks?.length) {
      return json({
        error: 'no_context',
        message: 'No relevant documents found. The ingest pipeline may not have run yet.',
      });
    }

    // ── Sarvam ────────────────────────────────────────────────────────────────

    let sarvamRes;
    try {
      sarvamRes = await fetch('https://api.sarvam.ai/v1/chat/completions', {
        method: 'POST',
        headers: {
          'api-subscription-key': process.env.SARVAM_API_KEY,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          model: 'sarvam-m',
          messages: [
            {
              role: 'system',
              content:
                'You are a GST regulatory foresight analyst for India. Provide structured, evidence-grounded assessments of upcoming GST regulatory changes based strictly on the documents provided.',
            },
            { role: 'user', content: buildPrompt(cleanQuery, chunks) },
          ],
          temperature: 0.2,
          max_tokens: 1024,
          stream: false,
        }),
      });
    } catch (e) {
      console.error('[sarvam] fetch failed:', e);
      return json({ error: 'llm_unavailable', message: 'Analysis service temporarily unavailable.' }, 502);
    }

    if (!sarvamRes.ok) {
      console.error('[sarvam] error response:', await sarvamRes.text());
      return json({ error: 'llm_error', message: 'Failed to generate analysis.' }, 502);
    }

    const sarvamData = await sarvamRes.json();
    const answer = sarvamData.choices?.[0]?.message?.content ?? '';

    return json({
      answer,
      sources: chunks.slice(0, 4).map((c) => ({
        source_id: c.source_id,
        date: c.date,
        topic_tags: c.topic_tags,
        excerpt: (c.content ?? '').slice(0, 250),
      })),
      remaining_queries: rl?.remaining ?? null,
      query: cleanQuery,
    });
  },
};
